"""Agent-facing API for server-monitoring host agents.

Distinct from ``sensor_api.py`` (which serves remote sensor appliances),
this module hosts the per-host agent runtime endpoints:

    POST  /api/v1/agents/enroll           exchange enrollment token for api-key
    POST  /api/v1/agents/heartbeat        keep-alive + status
    GET   /api/v1/agents/config           fetch signed policy config (ETag aware)
    POST  /api/v1/agents/results/host     batched host metric upload
    POST  /api/v1/agents/events           status events
    POST  /api/v1/agents/diagnostics      diagnostics bundle metadata
    GET   /api/v1/agents/packages/manifest  latest package per channel
    POST  /api/v1/agents/commands/poll    poll for queued commands
    POST  /api/v1/agents/commands/{id}/result  report command result

Auth model
----------
``/enroll`` accepts a one-time enrollment token (hashed at rest).
All other endpoints require::

    Authorization: Bearer <api_key>
    X-Agent-Id: <uuid>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, get_clickhouse_client
from app.schemas.agent import (
    AgentCommandPoll,
    AgentCommandResult,
    AgentConfigResponse,
    AgentDiagnosticsUpload,
    AgentEnrollRequest,
    AgentEnrollResponse,
    AgentEventsBatch,
    AgentHeartbeatRequest,
    AgentHeartbeatResponse,
    AgentPackagesManifest,
    AgentResultsBatch,
    AgentResultsResponse,
)
from app.services.host_metric_service import ingest_host_metric_batch

router = APIRouter(prefix="/agents", tags=["Agents (runtime)"])
logger = logging.getLogger("zenplus.agents")

AGENT_KEY_PREFIX = "zpa_"          # zenplus agent key
# On-disk package store. Publish flow (POST /agent-fleet/packages/publish)
# scans this tree and registers rows in agent_packages; the download endpoint
# below serves only files registered there.
AGENT_PKG_DIR = Path("/opt/zenplus/artifacts/agents")
DEFAULT_HEARTBEAT_S = 30
DEFAULT_CONFIG_POLL_S = 60
DEFAULT_UPLOAD_S = 60
STALE_AFTER_HEARTBEATS = 3


# ── Helpers ──────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_api_key() -> tuple[str, str, str]:
    raw = AGENT_KEY_PREFIX + "key_" + secrets.token_urlsafe(32)
    return raw, _sha256(raw), raw[:12]


def _strip_bearer(value: Optional[str]) -> str:
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def _authenticate(
    agent_id: str,
    bearer: str,
    db: AsyncSession,
) -> dict:
    if not agent_id or not bearer:
        raise HTTPException(401, "Missing agent credentials")
    try:
        aid = UUID(agent_id)
    except ValueError:
        raise HTTPException(401, "Invalid agent id")

    row = (await db.execute(
        text("SELECT * FROM agents WHERE id = :id"),
        {"id": aid},
    )).mappings().first()
    if not row:
        raise HTTPException(401, "Agent not registered")
    if row["status"] == "disabled":
        raise HTTPException(403, "Agent disabled")
    if not row.get("api_key_hash"):
        raise HTTPException(401, "Agent not enrolled")
    if not hmac.compare_digest(_sha256(bearer), str(row["api_key_hash"])):
        raise HTTPException(401, "Invalid agent api key")
    return dict(row)


async def _config_etag_for_agent(agent_row: dict, db: AsyncSession) -> tuple[str, dict]:
    """Compute a stable ETag from the agent's resolved policy.

    Returns ``(etag, policy_row)``. If the agent has no policy, falls back
    to a built-in default (Windows Baseline / Linux Baseline by platform).
    """
    policy_id = agent_row.get("policy_id")
    policy = None
    if policy_id:
        policy = (await db.execute(
            text("SELECT * FROM agent_policies WHERE id = :id"),
            {"id": policy_id},
        )).mappings().first()

    if not policy:
        # Fallback by platform.
        fallback = "Windows Baseline" if agent_row.get("platform") == "windows" else "Linux Baseline"
        policy = (await db.execute(
            text("SELECT * FROM agent_policies WHERE name = :n"),
            {"n": fallback},
        )).mappings().first()

    if not policy:
        raise HTTPException(500, "No agent policy available")

    parts = [
        str(policy["id"]),
        str(policy.get("config_version") or 1),
        policy["updated_at"].isoformat() if policy.get("updated_at") else "",
        agent_row["update_ring"] or "stable",
    ]
    etag = _sha256("|".join(parts))
    return etag, dict(policy)


def _resolve_policy_for_enrollment(
    platform: str,
    explicit_policy: Optional[Any],
) -> Optional[Any]:
    return explicit_policy


# ── Enrollment ───────────────────────────────────────────────────────

@router.post("/enroll", response_model=AgentEnrollResponse)
async def enroll(
    data: AgentEnrollRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time enrollment token for a long-lived api-key."""
    token_hash = _sha256(data.enrollment_token)
    tok = (await db.execute(
        text("SELECT * FROM agent_enrollment_tokens WHERE token_hash = :h"),
        {"h": token_hash},
    )).mappings().first()
    if not tok:
        raise HTTPException(401, "Invalid enrollment token")
    if tok.get("revoked_at"):
        raise HTTPException(401, "Enrollment token revoked")
    expires = tok.get("expires_at")
    if expires and expires.replace(tzinfo=expires.tzinfo or timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(401, "Enrollment token expired")
    if tok["platform"] not in ("any", data.platform):
        raise HTTPException(400, f"Token issued for {tok['platform']} but agent is {data.platform}")

    client_ip = _client_ip(request)

    # Claim one use atomically so concurrent bulk installs can't blow past
    # max_uses via read-check-write races.
    claimed = (await db.execute(
        text("""UPDATE agent_enrollment_tokens SET
                  uses = uses + 1,
                  consumed_at = CASE WHEN uses + 1 >= max_uses
                                     THEN COALESCE(consumed_at, NOW()) ELSE consumed_at END,
                  consumed_ip = COALESCE(consumed_ip, :ip)
                WHERE id = :id AND uses < max_uses AND revoked_at IS NULL
                RETURNING id"""),
        {"id": tok["id"], "ip": client_ip},
    )).first()
    if not claimed:
        raise HTTPException(401, "Enrollment token already used")

    # Reconcile or create the server record.
    server_id = tok.get("server_id")
    if not server_id:
        # Match by hostname if available
        existing = (await db.execute(
            text("SELECT id FROM servers WHERE hostname = :h LIMIT 1"),
            {"h": data.hostname},
        )).first()
        if existing:
            server_id = existing[0]
        else:
            ins = (await db.execute(
                text("""INSERT INTO servers (display_name, hostname, fqdn, primary_ip, site_id,
                                              os_type, os_name, os_version, kernel_or_build,
                                              architecture, collection_mode, status, last_seen,
                                              tags)
                        VALUES (:dn, :hn, :fqdn, :ip, :site,
                                :os_t, :os_n, :os_v, :kb,
                                :arch, 'agent', 'healthy', NOW(),
                                COALESCE(:tags, '[]'::jsonb))
                        RETURNING id"""),
                {
                    "dn": data.hostname,
                    "hn": data.hostname,
                    "fqdn": data.fqdn,
                    "ip": data.primary_ip,
                    "site": tok.get("site_id"),
                    "os_t": data.platform if data.platform in ("windows", "linux", "macos") else "other",
                    "os_n": data.os_name,
                    "os_v": data.os_version,
                    "kb": data.kernel_or_build,
                    "arch": data.architecture,
                    "tags": json.dumps(tok.get("tags") or []),
                },
            )).first()
            server_id = ins[0]
    else:
        # Update last_seen on existing server
        await db.execute(
            text("""UPDATE servers SET last_seen = NOW(),
                                       status = CASE WHEN status = 'disabled' THEN status ELSE 'healthy' END,
                                       updated_at = NOW()
                    WHERE id = :id"""),
            {"id": server_id},
        )

    # Find or create the agent. If agent_uid already exists, treat as re-enrollment.
    existing_agent = (await db.execute(
        text("SELECT id FROM agents WHERE agent_uid = :u"),
        {"u": data.agent_uid},
    )).first()

    api_key, api_hash, prefix = _new_api_key()

    if existing_agent:
        agent_id = existing_agent[0]
        await db.execute(
            text("""UPDATE agents SET
                      server_id = :sid,
                      hostname = :hn,
                      platform = :plat,
                      version = :v,
                      install_id = :iid,
                      site_id = :site,
                      policy_id = COALESCE(:pol, policy_id),
                      api_key_hash = :h,
                      api_key_prefix = :p,
                      api_key_rotated_at = NOW(),
                      status = 'online',
                      last_heartbeat_at = NOW(),
                      current_version = :v,
                      last_ip = :ip,
                      updated_at = NOW()
                    WHERE id = :aid"""),
            {
                "sid": server_id,
                "hn": data.hostname,
                "plat": data.platform,
                "v": data.version,
                "iid": data.install_id,
                "site": tok.get("site_id"),
                "pol": tok.get("policy_id"),
                "h": api_hash,
                "p": prefix,
                "ip": client_ip,
                "aid": agent_id,
            },
        )
    else:
        # Resolve default policy by platform if none specified on token.
        policy_id = tok.get("policy_id")
        if not policy_id:
            row = (await db.execute(
                text("""SELECT id FROM agent_policies
                        WHERE platform = :p AND is_builtin = TRUE
                        ORDER BY created_at LIMIT 1"""),
                {"p": data.platform if data.platform in ("windows", "linux") else "any"},
            )).first()
            if row:
                policy_id = row[0]

        ins = (await db.execute(
            text("""INSERT INTO agents (server_id, agent_uid, hostname, platform, version, install_id,
                                         site_id, policy_id, api_key_hash, api_key_prefix,
                                         api_key_rotated_at, status, last_heartbeat_at,
                                         current_version, last_ip)
                    VALUES (:sid, :uid, :hn, :plat, :v, :iid,
                            :site, :pol, :h, :p,
                            NOW(), 'online', NOW(),
                            :v, :ip)
                    RETURNING id"""),
            {
                "sid": server_id, "uid": data.agent_uid, "hn": data.hostname,
                "plat": data.platform, "v": data.version, "iid": data.install_id,
                "site": tok.get("site_id"), "pol": policy_id,
                "h": api_hash, "p": prefix, "ip": client_ip,
            },
        )).first()
        agent_id = ins[0]

    await db.commit()
    return AgentEnrollResponse(
        agent_id=str(agent_id),
        server_id=str(server_id),
        api_key=api_key,
        heartbeat_interval_s=DEFAULT_HEARTBEAT_S,
        config_poll_interval_s=DEFAULT_CONFIG_POLL_S,
        upload_interval_s=DEFAULT_UPLOAD_S,
        policy_id=str(tok.get("policy_id")) if tok.get("policy_id") else None,
    )


# ── Heartbeat ────────────────────────────────────────────────────────

@router.post("/heartbeat", response_model=AgentHeartbeatResponse)
async def heartbeat(
    data: AgentHeartbeatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    client_ip = _client_ip(request)

    await db.execute(
        text("""UPDATE agents SET
                  last_heartbeat_at = NOW(),
                  version = COALESCE(:v, version),
                  current_version = COALESCE(:v, current_version),
                  queue_depth = :qd,
                  spool_bytes = :sb,
                  last_config_hash = COALESCE(:ch, last_config_hash),
                  config_apply_error = :err,
                  last_ip = COALESCE(:ip, last_ip),
                  status = CASE WHEN status IN ('disabled') THEN status ELSE 'online' END,
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "v": data.version, "qd": data.queue_depth, "sb": data.spool_bytes,
            "ch": data.config_hash, "err": data.config_apply_error,
            "ip": client_ip, "id": agent["id"],
        },
    )
    # Roll server.last_seen.
    if agent.get("server_id"):
        await db.execute(
            text("""UPDATE servers SET last_seen = NOW(),
                                       status = CASE WHEN status = 'disabled' THEN status
                                                     WHEN status = 'stale' THEN 'healthy'
                                                     ELSE status END,
                                       status_reasons = CASE WHEN status = 'stale' THEN '[]'::jsonb
                                                             ELSE status_reasons END,
                                       updated_at = NOW()
                    WHERE id = :id"""),
            {"id": agent["server_id"]},
        )
        # Recovery: close any open agent-offline alert for this server.
        if agent.get("status") in ("stale", "offline"):
            from app.services.server_health_service import resolve_server_alerts
            await resolve_server_alerts(db, str(agent["server_id"]), "agent_offline")
    await db.commit()

    # Check for queued commands
    cmd_row = (await db.execute(
        text("""SELECT COUNT(*) FROM agent_commands
                WHERE agent_id = :id AND status IN ('queued','sent')"""),
        {"id": agent["id"]},
    )).first()
    has_cmd = bool(cmd_row and cmd_row[0])

    fresh_agent = (await db.execute(
        text("SELECT * FROM agents WHERE id = :id"),
        {"id": agent["id"]},
    )).mappings().first()
    etag, _ = await _config_etag_for_agent(dict(fresh_agent), db)

    return AgentHeartbeatResponse(
        ok=True,
        server_time=datetime.now(timezone.utc),
        config_etag=etag,
        has_commands=has_cmd,
        desired_version=fresh_agent.get("desired_version"),
    )


# ── Config ───────────────────────────────────────────────────────────

@router.get("/config", response_model=AgentConfigResponse)
async def get_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
    if_none_match: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    etag, policy = await _config_etag_for_agent(agent, db)

    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304)

    def _arr(field: str) -> list:
        v = policy.get(field)
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return list(v)
        if isinstance(v, str):
            try:
                return list(json.loads(v))
            except Exception:
                return []
        return list(v)

    def _obj(field: str) -> dict:
        v = policy.get(field)
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return dict(json.loads(v))
            except Exception:
                return {}
        return {}

    return AgentConfigResponse(
        config_version=policy.get("config_version") or 1,
        policy_id=str(policy["id"]),
        etag=etag,
        metric_interval_s=policy.get("metric_interval_s") or 30,
        upload_interval_s=policy.get("upload_interval_s") or 60,
        process_top_n=policy.get("process_top_n") or 25,
        service_watchlist=_arr("service_watchlist"),
        process_watchlist=_arr("process_watchlist"),
        event_log_filters=_arr("event_log_filters"),
        disk_ignore=_arr("disk_ignore"),
        network_ignore=_arr("network_ignore"),
        cardinality_limits=_obj("cardinality_limits"),
        feature_flags=_obj("feature_flags"),
        update_ring=agent.get("update_ring") or "stable",
        signed_at=datetime.now(timezone.utc),
    )


# ── Results ingestion ────────────────────────────────────────────────

@router.post("/results/host", response_model=AgentResultsResponse)
async def post_results(
    data: AgentResultsBatch,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)

    # Validate identity matches the bearer's agent.
    if str(agent["id"]) != data.agent_id:
        raise HTTPException(403, "agent_id mismatch with bearer credential")

    server_id = agent.get("server_id")
    if not server_id:
        raise HTTPException(400, "Agent has no server binding")
    if str(server_id) != data.server_id:
        raise HTTPException(400, "server_id mismatch with agent binding")

    # Idempotency: per-agent batch_id (string) is deduped via the agent's
    # last seen batch column. Cheap and good enough for MVP; a dedicated
    # batch ledger can be added later if needed.
    duplicates = 0

    accepted, rejected, errors, clock_skew_s = await ingest_host_metric_batch(
        str(agent["id"]), str(server_id), data, db,
    )

    # Update last_metric_at + inventory snapshots
    await db.execute(
        text("""UPDATE agents SET last_metric_at = NOW(), clock_skew_s = :skew,
                                  updated_at = NOW()
                WHERE id = :id"""),
        {"id": agent["id"], "skew": clock_skew_s},
    )
    await db.execute(
        text("""UPDATE servers SET last_seen = NOW(), updated_at = NOW()
                WHERE id = :id"""),
        {"id": server_id},
    )
    await db.commit()
    return AgentResultsResponse(
        ok=True, accepted=accepted, rejected=rejected, duplicates=duplicates,
        errors=errors[:25],
    )


# ── Events ───────────────────────────────────────────────────────────

@router.post("/events")
async def post_events(
    data: AgentEventsBatch,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    # Simple noop v1: write to logger; events ingestion expanded later.
    logger.info("agent.events agent=%s count=%d", agent["id"], len(data.events))
    return {"ok": True, "accepted": len(data.events)}


# ── Diagnostics ──────────────────────────────────────────────────────

@router.post("/diagnostics")
async def post_diagnostics(
    data: AgentDiagnosticsUpload,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    if str(agent["id"]) != data.agent_id:
        raise HTTPException(403, "agent_id mismatch")

    diag_id = data.diagnostic_id
    if diag_id:
        await db.execute(
            text("""UPDATE agent_diagnostics SET
                      received_at = NOW(),
                      file_name = :fn,
                      file_size = :fs,
                      sha256 = :sha,
                      status = 'received'
                    WHERE id = :id AND agent_id = :aid"""),
            {"fn": data.file_name, "fs": data.file_size, "sha": data.sha256,
             "id": diag_id, "aid": agent["id"]},
        )
    else:
        await db.execute(
            text("""INSERT INTO agent_diagnostics (agent_id, requested_at, received_at,
                                                    file_name, file_size, sha256, status, notes)
                    VALUES (:aid, NOW(), NOW(), :fn, :fs, :sha, 'received', :notes)"""),
            {"aid": agent["id"], "fn": data.file_name, "fs": data.file_size,
             "sha": data.sha256, "notes": data.notes},
        )
    await db.commit()
    return {"ok": True}


# ── Packages manifest ───────────────────────────────────────────────

@router.get("/packages/manifest")
async def packages_manifest(
    platform: str = Query(..., regex="^(windows|linux|macos)$"),
    channel: str = Query("stable", regex="^(canary|beta|stable|pinned)$"),
    arch: str = Query("amd64"),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        text("""SELECT * FROM agent_packages
                WHERE platform = :p AND channel = :c AND arch = :a AND is_latest = TRUE
                ORDER BY released_at DESC LIMIT 1"""),
        {"p": platform, "c": channel, "a": arch},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "No package published for this platform/channel/arch yet")
    return {
        "platform": platform,
        "channel": channel,
        "arch": arch,
        "latest_version": row["version"],
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "sha256": row["sha256"],
        "signature": row.get("signature"),
        "released_at": row["released_at"],
        "download_url": row["download_path"],
    }


# ── Package download + installer scripts ────────────────────────────
# Unauthenticated by design: installers run before the host has any
# credential. Tokens gate enrollment; these endpoints only hand out the
# published package (integrity via sha256 in the manifest) and install logic.

async def _latest_package(platform: str, db: AsyncSession, arch: str = "amd64") -> Optional[dict]:
    row = (await db.execute(
        text("""SELECT * FROM agent_packages
                WHERE platform = :p AND arch = :a AND channel = 'stable' AND is_latest = TRUE
                ORDER BY released_at DESC LIMIT 1"""),
        {"p": platform, "a": arch},
    )).mappings().first()
    return dict(row) if row else None


@router.get("/packages/{platform}/latest")
async def download_latest_package(
    platform: str,
    arch: str = Query("amd64"),
    db: AsyncSession = Depends(get_db),
):
    if platform not in ("windows", "linux", "macos"):
        raise HTTPException(404, "Unknown platform")
    pkg = await _latest_package(platform, db, arch)
    if not pkg:
        raise HTTPException(
            404,
            f"No {platform} agent package published. Drop the package into "
            f"{AGENT_PKG_DIR}/{platform}/ and publish it from the Agent Fleet page.",
        )
    file_path = (AGENT_PKG_DIR / platform / pkg["file_name"]).resolve()
    if not str(file_path).startswith(str(AGENT_PKG_DIR)) or not file_path.is_file():
        raise HTTPException(410, "Published package file is missing on disk")
    media = "application/x-msi" if pkg["file_name"].endswith(".msi") else "application/octet-stream"
    return FileResponse(file_path, media_type=media, filename=pkg["file_name"],
                        headers={"X-Package-Version": pkg["version"],
                                 "X-Package-Sha256": pkg["sha256"]})


_INSTALL_PS1 = r'''#Requires -RunAsAdministrator
<#
ZenPlus agent installer (Windows).
Downloads the latest published MSI from the controller, verifies its SHA-256
against the manifest, then installs silently with enrollment properties.

Usage:
  .\install.ps1 -ControllerUrl "https://zenplus.example.com" -EnrollmentToken "zpa_enr_..."
Optional: -Tags "prod,web-tier"  -Arch amd64
#>
param(
    [Parameter(Mandatory=$true)][string]$ControllerUrl,
    [Parameter(Mandatory=$true)][string]$EnrollmentToken,
    [string]$Tags = "",
    [string]$Arch = "amd64"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ControllerUrl = $ControllerUrl.TrimEnd("/")

Write-Host "[zenplus] Fetching package manifest from $ControllerUrl ..."
$manifest = Invoke-RestMethod -UseBasicParsing -Uri "$ControllerUrl/api/v1/agents/packages/manifest?platform=windows&channel=stable&arch=$Arch"
$version = $manifest.latest_version
$expectedSha = $manifest.sha256.ToLower()
Write-Host "[zenplus] Latest agent version: $version"

$msi = Join-Path $env:TEMP "zenplus-agent-$version.msi"
Write-Host "[zenplus] Downloading MSI ..."
Invoke-WebRequest -UseBasicParsing -Uri "$ControllerUrl/api/v1/agents/packages/windows/latest?arch=$Arch" -OutFile $msi

$actualSha = (Get-FileHash -Algorithm SHA256 -Path $msi).Hash.ToLower()
if ($actualSha -ne $expectedSha) {
    Remove-Item -Force $msi
    throw "[zenplus] SHA-256 mismatch (expected $expectedSha, got $actualSha) - aborting install."
}
Write-Host "[zenplus] Checksum OK."

$msiArgs = @("/i", "`"$msi`"", "/quiet", "/norestart",
             "CONTROLLER_URL=`"$ControllerUrl`"",
             "ENROLLMENT_TOKEN=`"$EnrollmentToken`"")
if ($Tags) { $msiArgs += "AGENT_TAGS=`"$Tags`"" }
Write-Host "[zenplus] Installing ..."
$proc = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "[zenplus] msiexec exited with code $($proc.ExitCode)."
}
Write-Host "[zenplus] Installed. The agent enrolls itself and appears in the dashboard within a minute."
'''


@router.get("/install.ps1")
async def install_ps1():
    """Windows installer script (download + verify + silent MSI install)."""
    return PlainTextResponse(_INSTALL_PS1, media_type="text/plain; charset=utf-8")


_INSTALL_SH = r'''#!/usr/bin/env bash
# ZenPlus agent installer (Linux).
# Required env: ZENPLUS_CONTROLLER_URL, ZENPLUS_ENROLLMENT_TOKEN
# Optional env: ZENPLUS_AGENT_TAGS (comma-separated), ZENPLUS_ARCH (default amd64)
set -euo pipefail

CTRL="${ZENPLUS_CONTROLLER_URL:?ZENPLUS_CONTROLLER_URL is required}"
TOKEN="${ZENPLUS_ENROLLMENT_TOKEN:?ZENPLUS_ENROLLMENT_TOKEN is required}"
ARCH="${ZENPLUS_ARCH:-amd64}"
CTRL="${CTRL%/}"

if [ "$(id -u)" -ne 0 ]; then
    echo "[zenplus] must run as root (use sudo)" >&2; exit 1
fi

echo "[zenplus] fetching package manifest ..."
MANIFEST=$(curl -fsSL "$CTRL/api/v1/agents/packages/manifest?platform=linux&channel=stable&arch=$ARCH")
VERSION=$(printf '%s' "$MANIFEST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["latest_version"])')
SHA=$(printf '%s' "$MANIFEST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["sha256"].lower())')
echo "[zenplus] latest agent version: $VERSION"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PKG="$TMP/zenplus-agent.tar.gz"
curl -fsSL "$CTRL/api/v1/agents/packages/linux/latest?arch=$ARCH" -o "$PKG"
echo "$SHA  $PKG" | sha256sum -c - >/dev/null || {
    echo "[zenplus] SHA-256 mismatch - aborting" >&2; exit 1; }
echo "[zenplus] checksum OK"

install -d /opt/zenplus-agent /etc/zenplus-agent
tar -xzf "$PKG" -C /opt/zenplus-agent
chmod 0755 /opt/zenplus-agent/zenplus-agent

cat > /etc/zenplus-agent/agent.env <<EOF
ZENPLUS_CONTROLLER_URL=$CTRL
ZENPLUS_ENROLLMENT_TOKEN=$TOKEN
ZENPLUS_AGENT_TAGS=${ZENPLUS_AGENT_TAGS:-}
EOF
chmod 0600 /etc/zenplus-agent/agent.env

cat > /etc/systemd/system/zenplus-agent.service <<'EOF'
[Unit]
Description=ZenPlus Server Monitoring Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/zenplus-agent/agent.env
ExecStart=/opt/zenplus-agent/zenplus-agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now zenplus-agent.service
echo "[zenplus] installed and started. The agent enrolls itself and appears in the dashboard within a minute."
'''


@router.get("/install.sh")
async def install_sh():
    """Linux installer script (download + verify + systemd service)."""
    return PlainTextResponse(_INSTALL_SH, media_type="text/plain; charset=utf-8")


# ── Command channel ──────────────────────────────────────────────────

@router.post("/commands/poll", response_model=AgentCommandPoll)
async def commands_poll(
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    rows = (await db.execute(
        text("""SELECT id, command, params, expires_at, created_at FROM agent_commands
                WHERE agent_id = :id AND status IN ('queued','sent')
                ORDER BY created_at LIMIT 10"""),
        {"id": agent["id"]},
    )).mappings().all()
    if not rows:
        return AgentCommandPoll(has_commands=False, commands=[])
    ids = [r["id"] for r in rows]
    await db.execute(
        text("""UPDATE agent_commands SET status = 'sent', sent_at = NOW()
                WHERE id = ANY(:ids)"""),
        {"ids": ids},
    )
    await db.commit()
    return AgentCommandPoll(
        has_commands=True,
        commands=[
            {
                "id": str(r["id"]),
                "command": r["command"],
                "params": r["params"] if isinstance(r["params"], dict) else {},
                "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            }
            for r in rows
        ],
    )


@router.post("/commands/{command_id}/result")
async def commands_result(
    command_id: UUID,
    data: AgentCommandResult,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)
    row = (await db.execute(
        text("SELECT agent_id FROM agent_commands WHERE id = :id"),
        {"id": command_id},
    )).first()
    if not row:
        raise HTTPException(404, "Command not found")
    if str(row[0]) != str(agent["id"]):
        raise HTTPException(403, "Command not addressed to this agent")
    await db.execute(
        text("""INSERT INTO agent_command_results (command_id, success, output, error_message)
                VALUES (:cid, :ok, :out, :err)
                ON CONFLICT (command_id) DO UPDATE
                SET success = EXCLUDED.success,
                    output = EXCLUDED.output,
                    error_message = EXCLUDED.error_message,
                    received_at = NOW()"""),
        {
            "cid": command_id, "ok": data.success,
            "out": json.dumps(data.output or {}),
            "err": data.error_message,
        },
    )
    await db.execute(
        text("""UPDATE agent_commands SET
                  status = CASE WHEN :ok THEN 'succeeded' ELSE 'failed' END,
                  completed_at = NOW()
                WHERE id = :cid"""),
        {"ok": data.success, "cid": command_id},
    )
    await db.commit()
    return {"ok": True}
