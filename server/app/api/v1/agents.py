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
``/enroll`` accepts either a one-time enrollment token or a tokenless pending
registration. Pending registrations cannot send monitoring data until an
operator authorizes them in Agent Fleet.
All other endpoints require::

    Authorization: Bearer <api_key>
    X-Agent-Id: <uuid>
"""

from __future__ import annotations

import asyncio
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
    NetworkCaptureUpload,
)
from app.services.host_metric_service import (
    HOST_TELEMETRY_KINDS,
    HostMetricStorageError,
    ingest_host_metric_batch,
)

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
HOST_RESULTS_LEDGER_RETENTION_DAYS = 7
HOST_RESULTS_LEDGER_CLEANUP_MODULUS = 256
# Keep the application deadline comfortably below the current Windows agent's
# 30-second HTTP timeout.  A timed-out transaction is rolled back, releasing
# the batch claim so the same id can be retried safely.
HOST_RESULTS_CLAIM_TIMEOUT_S = 5.0
HOST_RESULTS_INGEST_TIMEOUT_S = 20.0
# ``completed_at IS NULL`` rows should normally exist only inside an open
# transaction.  This lease also recovers incomplete rows committed by older
# releases or left behind by operational intervention.
HOST_RESULTS_IN_PROGRESS_TTL_S = 60
HOST_RESULTS_IN_PROGRESS_RETRY_AFTER_S = 5


# ── Helpers ──────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _host_results_batch_digest(batch: AgentResultsBatch) -> str:
    """Hash the immutable batch payload used to detect batch-id collisions.

    ``sent_at`` is deliberately excluded: the agent refreshes it immediately
    before every spool retry, while the batch identity and metric contents stay
    unchanged.
    """
    payload = batch.model_dump(mode="json", exclude={"sent_at"})
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return _sha256(canonical)


def _host_results_errors(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value][:25]


def _host_results_ledger_cleanup_due(agent_id: Any, batch_id: str) -> bool:
    """Spread bounded ledger cleanup deterministically across agent uploads."""
    digest = hashlib.sha256(f"{agent_id}:{batch_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % HOST_RESULTS_LEDGER_CLEANUP_MODULUS == 0


async def _prune_host_results_ledger(db: AsyncSession, agent_id: Any) -> None:
    await db.execute(
        text("""DELETE FROM agent_host_result_batches
                WHERE agent_id = :aid
                  AND completed_at < NOW() - make_interval(days => :days)"""),
        {"aid": agent_id, "days": HOST_RESULTS_LEDGER_RETENTION_DAYS},
    )


def _new_api_key() -> tuple[str, str, str]:
    raw = AGENT_KEY_PREFIX + "key_" + secrets.token_urlsafe(32)
    return raw, _sha256(raw), raw[:12]


def _strip_bearer(value: Optional[str]) -> str:
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def _same_uuid(left: Any, right: Any) -> bool:
    """Compare UUID identities independent of harmless text formatting."""
    try:
        return UUID(str(left)) == UUID(str(right))
    except (TypeError, ValueError, AttributeError):
        return False


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def _sync_server_identity(
    db: AsyncSession,
    server_id: Any,
    *,
    hostname: Optional[str],
    primary_ip: Optional[str],
    fqdn: Optional[str] = None,
    os_type: Optional[str] = None,
    os_name: Optional[str] = None,
    os_version: Optional[str] = None,
    kernel_or_build: Optional[str] = None,
    architecture: Optional[str] = None,
) -> None:
    """Keep an agent-managed server's factual identity current.

    ``display_name`` is an operator-controlled friendly name.  It follows the
    hostname only while it still contains an automatically generated identity
    (the old hostname, FQDN, or IP); deliberate custom labels are preserved.
    """
    await db.execute(
        text("""UPDATE servers SET
                  display_name = CASE
                    WHEN NULLIF(BTRIM(:hostname), '') IS NOT NULL
                     AND (NULLIF(BTRIM(display_name), '') IS NULL
                          OR display_name = hostname
                          OR display_name = fqdn
                          OR display_name = host(primary_ip))
                      THEN BTRIM(:hostname)
                    ELSE display_name
                  END,
                  hostname = COALESCE(NULLIF(BTRIM(:hostname), ''), hostname),
                  fqdn = COALESCE(NULLIF(BTRIM(:fqdn), ''), fqdn),
                  primary_ip = COALESCE(CAST(NULLIF(BTRIM(:ip), '') AS inet), primary_ip),
                  os_type = COALESCE(NULLIF(BTRIM(:os_type), ''), os_type),
                  os_name = COALESCE(NULLIF(BTRIM(:os_name), ''), os_name),
                  os_version = COALESCE(NULLIF(BTRIM(:os_version), ''), os_version),
                  kernel_or_build = COALESCE(NULLIF(BTRIM(:kernel), ''), kernel_or_build),
                  architecture = COALESCE(NULLIF(BTRIM(:architecture), ''), architecture),
                  collection_mode = 'agent',
                  updated_at = NOW()
                WHERE id = :server_id"""),
        {
            "server_id": server_id,
            "hostname": hostname,
            "fqdn": fqdn,
            "ip": primary_ip,
            "os_type": os_type,
            "os_name": os_name,
            "os_version": os_version,
            "kernel": kernel_or_build,
            "architecture": architecture,
        },
    )


def _capability_list(value: Any) -> list[str]:
    """Return a small, normalized capability set safe to persist/return."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for raw in value[:100]:
        capability = str(raw).strip().lower()[:64]
        if capability and capability not in result:
            result.append(capability)
    return result


def _package_media_type(file_name: str) -> str:
    lowered = file_name.lower()
    if lowered.endswith(".msi"):
        return "application/x-msi"
    if lowered.endswith(".pkg"):
        return "application/vnd.apple.installer+xml"
    if lowered.endswith(".tar.gz"):
        return "application/gzip"
    return "application/octet-stream"


def _verified_package_path(platform: str, package: dict) -> Path:
    """Resolve and verify an immutable published package before serving it."""
    package_root = AGENT_PKG_DIR.resolve()
    file_path = (package_root / platform / package["file_name"]).resolve()
    try:
        file_path.relative_to(package_root)
    except ValueError:
        raise HTTPException(410, "Published package path is outside the artifact store")
    if not file_path.is_file():
        raise HTTPException(410, "Published package file is missing on disk")

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha = digest.hexdigest()
    actual_size = file_path.stat().st_size
    if actual_sha != str(package["sha256"]).lower() or actual_size != int(package["file_size"]):
        logger.error(
            "refusing mutated agent package platform=%s version=%s file=%s "
            "expected_sha=%s actual_sha=%s",
            platform, package.get("version"), package["file_name"],
            package["sha256"], actual_sha,
        )
        raise HTTPException(
            409,
            "Published package failed integrity verification. Publish changed "
            "bytes under a new version before downloading.",
        )
    return file_path


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
    if row.get("revoked_at") is not None:
        raise HTTPException(403, "Agent authorization revoked")
    if row.get("authorized_at") is None:
        raise HTTPException(403, "Agent awaiting authorization")
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


async def _claim_enrollment_token(
    db: AsyncSession,
    *,
    token_hash: str,
    agent_uid: str,
    platform: str,
    client_ip: Optional[str],
) -> tuple[dict, bool]:
    """Claim one unique host slot, allowing idempotent enrollment retries.

    Locking the token row serializes concurrent first claims. A retry for the
    same ``token_id + agent_uid`` updates claim audit metadata but never
    increments ``uses``, even when a fixed-use token is already at capacity.
    """
    tok = (await db.execute(
        text("""SELECT * FROM agent_enrollment_tokens
                WHERE token_hash = :h FOR UPDATE"""),
        {"h": token_hash},
    )).mappings().first()
    if not tok:
        raise HTTPException(401, "Invalid enrollment token")
    if tok.get("revoked_at"):
        raise HTTPException(401, "Enrollment token revoked")
    expires = tok.get("expires_at")
    if expires and expires.replace(tzinfo=expires.tzinfo or timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(401, "Enrollment token expired")
    if tok["platform"] not in ("any", platform):
        raise HTTPException(400, f"Token issued for {tok['platform']} but agent is {platform}")

    normalized_uid = agent_uid.strip()
    previous = (await db.execute(
        text("""SELECT token_id FROM agent_enrollment_claims
                WHERE token_id = :token_id AND agent_uid = :agent_uid"""),
        {"token_id": tok["id"], "agent_uid": normalized_uid},
    )).first()
    if previous:
        await db.execute(
            text("""UPDATE agent_enrollment_claims SET
                      attempts = attempts + 1,
                      last_claimed_at = NOW(),
                      last_ip = :ip
                    WHERE token_id = :token_id AND agent_uid = :agent_uid"""),
            {"token_id": tok["id"], "agent_uid": normalized_uid, "ip": client_ip},
        )
        return dict(tok), False

    if tok["max_uses"] > 0 and tok["uses"] >= tok["max_uses"]:
        raise HTTPException(401, "Enrollment token has no uses left")

    await db.execute(
        text("""INSERT INTO agent_enrollment_claims
                  (token_id, agent_uid, first_ip, last_ip)
                VALUES (:token_id, :agent_uid, :ip, :ip)"""),
        {"token_id": tok["id"], "agent_uid": normalized_uid, "ip": client_ip},
    )
    claimed = (await db.execute(
        text("""UPDATE agent_enrollment_tokens SET
                  uses = uses + 1,
                  consumed_at = CASE WHEN max_uses > 0 AND uses + 1 >= max_uses
                                     THEN COALESCE(consumed_at, NOW()) ELSE consumed_at END,
                  consumed_ip = COALESCE(consumed_ip, :ip)
                WHERE id = :id AND revoked_at IS NULL
                RETURNING id"""),
        {"id": tok["id"], "ip": client_ip},
    )).first()
    if not claimed:
        raise HTTPException(409, "Enrollment token changed while it was being claimed")
    return dict(tok), True


# ── Enrollment ───────────────────────────────────────────────────────

async def _default_policy_id(platform: str, db: AsyncSession):
    row = (await db.execute(
        text("""SELECT id FROM agent_policies
                WHERE platform = :p AND is_builtin = TRUE
                ORDER BY created_at LIMIT 1"""),
        {"p": platform if platform in ("windows", "linux") else "any"},
    )).first()
    return row[0] if row else None


async def _ensure_pending_server(data: AgentEnrollRequest, db: AsyncSession):
    existing = (await db.execute(
        text("SELECT id FROM servers WHERE hostname = :h LIMIT 1"),
        {"h": data.hostname},
    )).first()
    if existing:
        return existing[0]
    row = (await db.execute(
        text("""INSERT INTO servers
                  (display_name, hostname, fqdn, primary_ip, os_type, os_name,
                   os_version, kernel_or_build, architecture, collection_mode,
                   status, tags)
                VALUES (:dn, :hn, :fqdn, :ip, :os_t, :os_n,
                        :os_v, :kb, :arch, 'agent', 'unknown', '[]'::jsonb)
                RETURNING id"""),
        {
            "dn": data.hostname, "hn": data.hostname, "fqdn": data.fqdn,
            "ip": data.primary_ip,
            "os_t": data.platform if data.platform in ("windows", "linux", "macos") else "other",
            "os_n": data.os_name, "os_v": data.os_version,
            "kb": data.kernel_or_build, "arch": data.architecture,
        },
    )).first()
    return row[0]


async def _enroll_pending_agent(
    data: AgentEnrollRequest,
    client_ip: Optional[str],
    db: AsyncSession,
) -> AgentEnrollResponse:
    if not data.pending_secret:
        raise HTTPException(400, "pending_secret required when enrollment_token is omitted")
    agent_uid = data.agent_uid.strip()
    pending_hash = _sha256(data.pending_secret)
    assigned_agent_uid: Optional[str] = None
    current = (await db.execute(
        text("SELECT * FROM agents WHERE agent_uid = :uid FOR UPDATE"),
        {"uid": agent_uid},
    )).mappings().first()

    if not current:
        policy_id = await _default_policy_id(data.platform, db)
        row = (await db.execute(
            text("""INSERT INTO agents
                      (agent_uid, hostname, platform, version, install_id,
                       policy_id, pending_secret_hash, authorization_source,
                       status, current_version, last_ip)
                    VALUES (:uid, :hn, :plat, :v, :iid,
                            :pol, :pending, 'pending',
                            'enrolling', :v, :ip)
                    RETURNING id"""),
            {
                "uid": agent_uid, "hn": data.hostname, "plat": data.platform,
                "v": data.version, "iid": data.install_id, "pol": policy_id,
                "pending": pending_hash, "ip": client_ip,
            },
        )).first()
        await db.commit()
        return AgentEnrollResponse(
            agent_id=str(row[0]), authorization_state="pending",
            policy_id=str(policy_id) if policy_id else None,
        )

    current = dict(current)
    stored_pending = current.get("pending_secret_hash")
    if not stored_pending or not hmac.compare_digest(str(stored_pending), pending_hash):
        # An administrator can explicitly keep a cloned host as a new machine.
        # The durable resolution is bound to both the duplicated reported UID
        # and this installation's protected pending-secret hash. This allows
        # the controller to route only the reviewed candidate to its assigned
        # identity; IP address and hostname are never used as proof.
        resolution = (await db.execute(
            text("""SELECT action, assigned_agent_id, assigned_agent_uid
                    FROM agent_registration_resolutions
                    WHERE reported_agent_uid = :uid
                      AND pending_secret_hash = :pending
                    FOR UPDATE"""),
            {"uid": agent_uid, "pending": pending_hash},
        )).mappings().first()
        if resolution:
            await db.execute(
                text("""UPDATE agent_registration_resolutions SET
                          first_claimed_at = COALESCE(first_claimed_at, NOW()),
                          last_seen_at = NOW(), retry_count = retry_count + 1
                        WHERE reported_agent_uid = :uid
                          AND pending_secret_hash = :pending"""),
                {"uid": agent_uid, "pending": pending_hash},
            )
            if resolution["action"] == "block":
                await db.commit()
                raise HTTPException(403, "Registration candidate was blocked by an appliance administrator")
            assigned = (await db.execute(
                text("SELECT * FROM agents WHERE id = :id FOR UPDATE"),
                {"id": resolution["assigned_agent_id"]},
            )).mappings().first()
            if not assigned:
                await db.rollback()
                raise HTTPException(409, "Assigned clone identity is no longer available")
            current = dict(assigned)
            assigned_agent_uid = str(resolution["assigned_agent_uid"])
            stored_pending = current.get("pending_secret_hash")
            if not stored_pending or not hmac.compare_digest(str(stored_pending), pending_hash):
                await db.rollback()
                raise HTTPException(409, "Assigned clone installation secret no longer matches")

        if resolution:
            # The approved candidate is now handled against its separate agent
            # row. Continue through the normal pending/authorized response path.
            pass
        else:
            # A reinstalled/reset agent can keep the same stable agent_uid while
            # generating a new OS-protected pending secret. Persist the candidate
            # hash and safe identifying metadata before returning 409 so Agent
            # Fleet can explain and resolve the collision. This commit is
            # intentional: raising HTTPException would otherwise roll the update
            # back with the request transaction.
            await db.execute(
                text("""UPDATE agents SET
                          pending_conflict_secret_hash = CAST(:pending AS varchar),
                          registration_conflict_revision = CASE
                            WHEN pending_conflict_secret_hash IS DISTINCT FROM CAST(:pending AS varchar)
                              THEN gen_random_uuid()
                            ELSE COALESCE(registration_conflict_revision, gen_random_uuid())
                          END,
                          registration_conflict_at = NOW(),
                          registration_conflict_ip = :ip,
                          registration_conflict_attempts = registration_conflict_attempts + 1,
                          registration_conflict_install_id = :iid,
                          registration_conflict_hostname = :hn,
                          registration_conflict_version = :v,
                          updated_at = NOW()
                        WHERE id = :id"""),
                {
                    "pending": pending_hash,
                    "ip": client_ip,
                    "iid": data.install_id,
                    "hn": data.hostname,
                    "v": data.version,
                    "id": current["id"],
                },
            )
            await db.commit()
            raise HTTPException(409, "Pending registration belongs to another agent installation")

    await db.execute(
        text("""UPDATE agents SET hostname = :hn, platform = :plat,
                  version = :v, install_id = :iid,
                  pending_secret_hash = COALESCE(pending_secret_hash, :pending),
                  pending_conflict_secret_hash = NULL,
                  registration_conflict_revision = NULL,
                  registration_conflict_at = NULL,
                  registration_conflict_ip = NULL,
                  registration_conflict_attempts = 0,
                  registration_conflict_install_id = NULL,
                  registration_conflict_hostname = NULL,
                  registration_conflict_version = NULL,
                  current_version = :v, last_ip = :ip, updated_at = NOW()
                WHERE id = :id"""),
        {
            "hn": data.hostname, "plat": data.platform, "v": data.version,
            "iid": data.install_id, "pending": pending_hash,
            "ip": client_ip, "id": current["id"],
        },
    )

    if current.get("revoked_at") is not None:
        await db.commit()
        return AgentEnrollResponse(
            agent_id=str(current["id"]),
            server_id=str(current["server_id"]) if current.get("server_id") else None,
            assigned_agent_uid=assigned_agent_uid,
            authorization_state="revoked",
            policy_id=str(current["policy_id"]) if current.get("policy_id") else None,
        )
    if current.get("authorized_at") is None:
        await db.commit()
        return AgentEnrollResponse(
            agent_id=str(current["id"]),
            server_id=str(current["server_id"]) if current.get("server_id") else None,
            assigned_agent_uid=assigned_agent_uid,
            authorization_state="pending",
            policy_id=str(current["policy_id"]) if current.get("policy_id") else None,
        )

    server_id = current.get("server_id") or await _ensure_pending_server(data, db)
    await _sync_server_identity(
        db,
        server_id,
        hostname=data.hostname,
        primary_ip=client_ip or data.primary_ip,
        fqdn=data.fqdn,
        os_type=data.platform if data.platform in ("windows", "linux", "macos") else "other",
        os_name=data.os_name,
        os_version=data.os_version,
        kernel_or_build=data.kernel_or_build,
        architecture=data.architecture,
    )
    api_key, api_hash, prefix = _new_api_key()
    await db.execute(
        text("""UPDATE agents SET server_id = :sid,
                  api_key_hash = :hash, api_key_prefix = :prefix,
                  api_key_rotated_at = NOW(), status = 'enrolling',
                  revoked_at = NULL, revoked_by = NULL, updated_at = NOW()
                WHERE id = :id"""),
        {
            "sid": server_id, "hash": api_hash, "prefix": prefix,
            "id": current["id"],
        },
    )
    await db.commit()
    return AgentEnrollResponse(
        agent_id=str(current["id"]), server_id=str(server_id), api_key=api_key,
        assigned_agent_uid=assigned_agent_uid,
        authorization_state="authorized",
        heartbeat_interval_s=DEFAULT_HEARTBEAT_S,
        config_poll_interval_s=DEFAULT_CONFIG_POLL_S,
        upload_interval_s=DEFAULT_UPLOAD_S,
        policy_id=str(current["policy_id"]) if current.get("policy_id") else None,
    )


@router.post("/enroll", response_model=AgentEnrollResponse)
async def enroll(
    data: AgentEnrollRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Register pending, or exchange authorization for a long-lived API key."""
    client_ip = _client_ip(request)
    agent_uid = data.agent_uid.strip()
    enrollment_token = (data.enrollment_token or "").strip()
    if not enrollment_token:
        return await _enroll_pending_agent(data, client_ip, db)
    tok, _first_claim = await _claim_enrollment_token(
        db,
        token_hash=_sha256(enrollment_token),
        agent_uid=agent_uid,
        platform=data.platform,
        client_ip=client_ip,
    )

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

    await _sync_server_identity(
        db,
        server_id,
        hostname=data.hostname,
        primary_ip=client_ip or data.primary_ip,
        fqdn=data.fqdn,
        os_type=data.platform if data.platform in ("windows", "linux", "macos") else "other",
        os_name=data.os_name,
        os_version=data.os_version,
        kernel_or_build=data.kernel_or_build,
        architecture=data.architecture,
    )

    # Find or create the agent. If agent_uid already exists, treat as re-enrollment.
    existing_agent = (await db.execute(
        text("SELECT id FROM agents WHERE agent_uid = :u"),
        {"u": agent_uid},
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
                      pending_secret_hash = COALESCE(:pending, pending_secret_hash),
                      authorized_at = NOW(),
                      revoked_at = NULL,
                      revoked_by = NULL,
                      authorization_source = 'enrollment_token',
                      enrollment_token_prefix = :token_prefix,
                      pending_conflict_secret_hash = NULL,
                      registration_conflict_revision = NULL,
                      registration_conflict_at = NULL,
                      registration_conflict_ip = NULL,
                      registration_conflict_attempts = 0,
                      registration_conflict_install_id = NULL,
                      registration_conflict_hostname = NULL,
                      registration_conflict_version = NULL,
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
                "pending": _sha256(data.pending_secret) if data.pending_secret else None,
                "token_prefix": tok.get("token_prefix"),
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
                                         api_key_rotated_at, pending_secret_hash,
                                         authorized_at, authorization_source,
                                         enrollment_token_prefix, status, last_heartbeat_at,
                                         current_version, last_ip)
                    VALUES (:sid, :uid, :hn, :plat, :v, :iid,
                            :site, :pol, :h, :p,
                            NOW(), :pending,
                            NOW(), 'enrollment_token', :token_prefix, 'online', NOW(),
                            :v, :ip)
                    RETURNING id"""),
            {
                "sid": server_id, "uid": agent_uid, "hn": data.hostname,
                "plat": data.platform, "v": data.version, "iid": data.install_id,
                "site": tok.get("site_id"), "pol": policy_id,
                "h": api_hash, "p": prefix, "ip": client_ip,
                "pending": _sha256(data.pending_secret) if data.pending_secret else None,
                "token_prefix": tok.get("token_prefix"),
            },
        )).first()
        agent_id = ins[0]

    await db.commit()
    return AgentEnrollResponse(
        agent_id=str(agent_id),
        server_id=str(server_id),
        api_key=api_key,
        authorization_state="authorized",
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
    capabilities = (
        _capability_list(data.capabilities)
        if data.capabilities is not None
        else None
    )

    await db.execute(
        text("""UPDATE agents SET
                  last_heartbeat_at = NOW(),
                  version = COALESCE(:v, version),
                  current_version = COALESCE(:v, current_version),
                  queue_depth = :qd,
                  spool_bytes = :sb,
                  last_config_hash = COALESCE(:ch, last_config_hash),
                  config_apply_error = :err,
                  capabilities = CASE WHEN :caps_present
                                      THEN CAST(:caps AS jsonb)
                                      ELSE capabilities END,
                  apm_status = CASE WHEN :apm_present
                                    THEN CAST(:apm AS jsonb)
                                    ELSE apm_status END,
                  last_ip = COALESCE(:ip, last_ip),
                  status = CASE WHEN status IN ('disabled') THEN status ELSE 'online' END,
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "v": data.version, "qd": data.queue_depth, "sb": data.spool_bytes,
            "ch": data.config_hash, "err": data.config_apply_error,
            "caps_present": capabilities is not None,
            "caps": json.dumps(capabilities or []),
            "apm_present": data.apm is not None,
            "apm": json.dumps(data.apm.model_dump(mode="json") if data.apm else {}),
            "ip": client_ip, "id": agent["id"],
        },
    )
    # Roll server.last_seen.
    if agent.get("server_id"):
        await _sync_server_identity(
            db,
            agent["server_id"],
            hostname=agent.get("hostname"),
            primary_ip=client_ip,
        )
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
                WHERE agent_id = :id AND status IN ('queued','sent')
                  AND (expires_at IS NULL OR expires_at > NOW())"""),
        {"id": agent["id"]},
    )).first()
    has_cmd = bool(cmd_row and cmd_row[0])

    fresh_agent = (await db.execute(
        text("SELECT * FROM agents WHERE id = :id"),
        {"id": agent["id"]},
    )).mappings().first()
    etag, _ = await _config_etag_for_agent(dict(fresh_agent), db)

    # This field was introduced after the host heartbeat contract. Returning it
    # is backward-compatible (older agents ignore unknown response fields) and
    # lets newer combined agents distinguish a healthy local gateway from an
    # unavailable appliance writer during rolling upgrades.
    from app.api.v1 import apm_ingest as apm_ingest_api

    agent_credential_bound = False
    if data.apm is not None and data.apm.enabled:
        credential_row = (await db.execute(
            text("""SELECT EXISTS (
                        SELECT 1 FROM agent_apm_credentials credential
                        JOIN apm_ingest_keys ingest_key
                          ON ingest_key.id = credential.key_id
                        WHERE credential.agent_id = :agent_id
                          AND ingest_key.enabled = TRUE
                          AND ingest_key.revoked_at IS NULL
                    )"""),
            {"agent_id": agent["id"]},
        )).first()
        agent_credential_bound = bool(credential_row and credential_row[0])
    appliance_apm = apm_ingest_api.runtime_status()
    appliance_apm["agent_credential_bound"] = agent_credential_bound
    appliance_apm["ready_for_agent"] = bool(
        appliance_apm.get("available") and agent_credential_bound
    )
    if data.apm is not None and data.apm.enabled and not agent_credential_bound:
        if appliance_apm.get("state") != "unavailable":
            appliance_apm["state"] = "degraded"
        appliance_apm["message"] = (
            "APM is not ready for this agent because it has no scoped ingest "
            "credential; re-enroll the agent APM forwarder"
        )

    # This field was introduced after the host heartbeat contract. Returning it
    # is backward-compatible (older agents ignore unknown response fields) and
    # lets newer combined agents distinguish a healthy local gateway from an
    # unavailable appliance writer during rolling upgrades.
    from app.api.v1 import apm_ingest as apm_ingest_api

    agent_credential_bound = False
    if data.apm is not None and data.apm.enabled:
        credential_row = (await db.execute(
            text("""SELECT EXISTS (
                        SELECT 1 FROM agent_apm_credentials credential
                        JOIN apm_ingest_keys ingest_key
                          ON ingest_key.id = credential.key_id
                        WHERE credential.agent_id = :agent_id
                          AND ingest_key.enabled = TRUE
                          AND ingest_key.revoked_at IS NULL
                    )"""),
            {"agent_id": agent["id"]},
        )).first()
        agent_credential_bound = bool(credential_row and credential_row[0])
    appliance_apm = apm_ingest_api.runtime_status()
    appliance_apm["agent_credential_bound"] = agent_credential_bound
    appliance_apm["ready_for_agent"] = bool(
        appliance_apm.get("available") and agent_credential_bound
    )
    if data.apm is not None and data.apm.enabled and not agent_credential_bound:
        if appliance_apm.get("state") != "unavailable":
            appliance_apm["state"] = "degraded"
        appliance_apm["message"] = (
            "APM is not ready for this agent because it has no scoped ingest "
            "credential; re-enroll the agent APM forwarder"
        )

    return AgentHeartbeatResponse(
        ok=True,
        server_time=datetime.now(timezone.utc),
        config_etag=etag,
        has_commands=has_cmd,
        desired_version=fresh_agent.get("desired_version"),
        capabilities=_capability_list(fresh_agent.get("capabilities")),
        apm=appliance_apm,
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
    if not _same_uuid(agent["id"], data.agent_id):
        raise HTTPException(403, "agent_id mismatch with bearer credential")

    server_id = agent.get("server_id")
    if not server_id:
        raise HTTPException(400, "Agent has no server binding")
    if not _same_uuid(server_id, data.server_id):
        raise HTTPException(400, "server_id mismatch with agent binding")

    batch_id = data.batch_id.strip()
    if not batch_id or batch_id != data.batch_id or len(batch_id) > 255:
        raise HTTPException(400, "batch_id must be 1-255 non-whitespace characters")

    # Claim the batch inside this transaction. PostgreSQL's unique constraint
    # makes concurrent retries wait for the first transaction to finish. Once
    # the first request commits, response-loss retries observe the recorded
    # outcome and skip ClickHouse/inventory ingestion. A matching incomplete
    # row older than the lease TTL can be reclaimed atomically; the WHERE
    # predicate is re-evaluated after PostgreSQL obtains the conflicting row
    # lock, so only one concurrent retry can reclaim it. PostgreSQL and
    # ClickHouse are not atomic, so a crash or deadline can still replay an
    # insert; deterministic per-kind ClickHouse tokens and migrate-094's
    # deduplication window make that replay idempotent.
    payload_sha256 = _host_results_batch_digest(data)
    try:
        claim_result = await asyncio.wait_for(
            db.execute(
                text("""
                    INSERT INTO agent_host_result_batches
                        (agent_id, batch_id, server_id, payload_sha256,
                         sequence_start, sequence_end)
                    VALUES (:aid, :bid, :sid, :payload, :seq_start, :seq_end)
                    ON CONFLICT (agent_id, batch_id) DO UPDATE
                    SET created_at = NOW()
                    WHERE agent_host_result_batches.completed_at IS NULL
                      AND agent_host_result_batches.created_at
                            < NOW() - make_interval(secs => :stale_seconds)
                      AND agent_host_result_batches.server_id = EXCLUDED.server_id
                      AND agent_host_result_batches.payload_sha256 = EXCLUDED.payload_sha256
                    RETURNING 1
                """),
                {
                    "aid": agent["id"], "bid": batch_id, "sid": server_id,
                    "payload": payload_sha256,
                    "seq_start": data.sequence_start,
                    "seq_end": data.sequence_end,
                    "stale_seconds": HOST_RESULTS_IN_PROGRESS_TTL_S,
                },
            ),
            timeout=HOST_RESULTS_CLAIM_TIMEOUT_S,
        )
    except TimeoutError as exc:
        # A retry can block on the unique key while the original transaction
        # is still ingesting. Bound that wait independently so it cannot
        # inherit nginx's broad /api timeout.
        await db.rollback()
        logger.warning(
            "host-results claim deadline exceeded agent=%s batch=%s timeout_s=%s",
            agent["id"], batch_id, HOST_RESULTS_CLAIM_TIMEOUT_S,
        )
        raise HTTPException(
            503,
            "Host-results batch claim is busy; retry the same batch",
            headers={"Retry-After": "2"},
        ) from exc
    claimed = claim_result.first()

    if not claimed:
        existing = (await db.execute(
            text("""
                SELECT server_id, payload_sha256, accepted, rejected, errors,
                       created_at, completed_at
                FROM agent_host_result_batches
                WHERE agent_id = :aid AND batch_id = :bid
            """),
            {"aid": agent["id"], "bid": batch_id},
        )).mappings().first()
        if existing and (
            str(existing.get("server_id")) != str(server_id)
            or not hmac.compare_digest(
                str(existing.get("payload_sha256") or ""), payload_sha256,
            )
        ):
            await db.rollback()
            raise HTTPException(409, "batch_id was already used for a different payload")
        if not existing or existing.get("completed_at") is None:
            await db.rollback()
            raise HTTPException(
                409,
                "host-results batch is still being processed; retry later",
                headers={
                    "Retry-After": str(HOST_RESULTS_IN_PROGRESS_RETRY_AFTER_S),
                },
            )

        accepted_before = max(0, int(existing.get("accepted") or 0))
        rejected_before = max(0, int(existing.get("rejected") or 0))
        errors_before = _host_results_errors(existing.get("errors"))
        await db.commit()
        logger.info(
            "agent results duplicate agent=%s batch=%s accepted_before=%d",
            agent["id"], batch_id, accepted_before,
        )
        return AgentResultsResponse(
            ok=True,
            accepted=0,
            rejected=rejected_before,
            duplicates=accepted_before,
            errors=errors_before,
        )

    try:
        accepted, rejected, errors, clock_skew_s = await asyncio.wait_for(
            ingest_host_metric_batch(
                str(agent["id"]), str(server_id), data, db,
            ),
            timeout=HOST_RESULTS_INGEST_TIMEOUT_S,
        )
    except TimeoutError as exc:
        # The ledger claim is part of this transaction, so rollback makes the
        # same batch id immediately claimable. A ClickHouse worker thread may
        # finish after cancellation; its stable deduplication token makes the
        # subsequent replay harmless.
        await db.rollback()
        logger.warning(
            "host-results ingest deadline exceeded agent=%s batch=%s timeout_s=%s",
            agent["id"], batch_id, HOST_RESULTS_INGEST_TIMEOUT_S,
        )
        raise HTTPException(
            503,
            "Host telemetry processing timed out; retry the same batch",
            headers={"Retry-After": "2"},
        ) from exc
    except HostMetricStorageError as exc:
        # Do not finalize the durable ledger or acknowledge the batch. The
        # Windows agent retains it in its spool on 503 and can replay it after
        # ClickHouse/Postgres schema convergence completes.
        await db.rollback()
        logger.warning(
            "retryable host-results storage failure agent=%s batch=%s part=%s",
            agent["id"], batch_id, exc.part,
        )
        raise HTTPException(
            503,
            "Host telemetry storage is unavailable; retry the same batch",
            headers={"Retry-After": "2"},
        ) from exc
    response_errors = errors[:25]

    await db.execute(
        text("""
            UPDATE agent_host_result_batches
            SET accepted = :accepted,
                rejected = :rejected,
                errors = CAST(:errors AS JSONB),
                clock_skew_s = :skew,
                completed_at = NOW()
            WHERE agent_id = :aid AND batch_id = :bid
        """),
        {
            "accepted": accepted, "rejected": rejected,
            "errors": json.dumps(response_errors), "skew": clock_skew_s,
            "aid": agent["id"], "bid": batch_id,
        },
    )

    # Agent self-health is connectivity/queue telemetry, not proof that the
    # Server module is delivering host signals.  Keep last_metric_at stable for
    # agent_health-only batches so heartbeat and host readiness remain distinct.
    has_host_telemetry = any(
        sample.kind in HOST_TELEMETRY_KINDS for sample in data.metrics
    )
    await db.execute(
        text("""UPDATE agents SET last_metric_at = CASE WHEN :has_host_telemetry
                                                        THEN NOW() ELSE last_metric_at END,
                                  clock_skew_s = :skew,
                                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "id": agent["id"], "skew": clock_skew_s,
            "has_host_telemetry": has_host_telemetry,
        },
    )
    # Heartbeats remain the connectivity signal for the server.  A results
    # upload may contain only agent self-health, so only a successfully stored
    # host signal may also advance the Server module's data-seen timestamp.
    if has_host_telemetry:
        await db.execute(
            text("""UPDATE servers SET last_seen = NOW(), updated_at = NOW()
                    WHERE id = :id"""),
            {"id": server_id},
        )
    if _host_results_ledger_cleanup_due(agent["id"], batch_id):
        await _prune_host_results_ledger(db, agent["id"])
    await db.commit()
    return AgentResultsResponse(
        ok=True, accepted=accepted, rejected=rejected, duplicates=0,
        errors=response_errors,
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

# ── Network capture ingest ──────────────────────────────────────────

@router.post("/network-capture")
async def post_network_capture(
    data: NetworkCaptureUpload,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """Receive flows from a running or finished capture.

    Called repeatedly while a capture runs so the dashboard can follow it.
    Flows are keyed by their 5-tuple in a ReplacingMergeTree, so a later
    upload supersedes the earlier partial rather than double-counting.
    """
    agent = await _authenticate(x_agent_id, _strip_bearer(authorization), db)

    capture_id = str(data.capture_id)

    row = (await db.execute(
        text("SELECT id, server_id, agent_id, status FROM network_captures WHERE id = :id"),
        {"id": capture_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Unknown capture")
    if str(row["server_id"]) != str(agent["server_id"]):
        # An agent may only report on captures for its own server.
        raise HTTPException(403, "Capture belongs to a different server")
    if row.get("agent_id") and str(row["agent_id"]) != str(agent["id"]):
        raise HTTPException(403, "Capture belongs to a different agent")
    if row["status"] in ("completed", "failed", "expired", "cancelled"):
        return {"ok": True, "ignored": True,
                "detail": f"Capture already {row['status']}"}

    flows = data.flows
    interfaces = data.interfaces
    status = data.status
    # A normal progress upload racing with a stop request must not move the
    # control row backwards from stopping to running.
    effective_status = (
        "stopping" if row["status"] == "stopping" and status == "running"
        else status
    )

    sent_total = sum(f.bytes_sent for f in flows)
    recv_total = sum(f.bytes_received for f in flows)

    now = datetime.now(timezone.utc)
    if flows:
        rows = []
        for f in flows:
            fields_set = getattr(f, "model_fields_set", None)
            if fields_set is None:  # Pydantic v1 compatibility
                fields_set = getattr(f, "__fields_set__", set())
            # Before classification fields existed, every uploaded row was a
            # peer connection (listeners were discarded). Preserve that
            # meaning when an older agent omits kind.
            flow_kind = f.kind if "kind" in fields_set else "connection"
            rows.append([
                capture_id, str(agent["server_id"]), str(agent["id"]), now,
                f.protocol[:8], f.direction, flow_kind, f.local_ip[:64], f.local_port,
                f.remote_ip[:64], f.remote_port, f.pid,
                f.process_name[:255], f.service_name[:255], f.state[:32],
                f.bytes_sent, f.bytes_received, 1 if f.bytes_known else 0,
                f.first_seen or now, f.last_seen or now, f.samples,
            ])
        if rows:
            await asyncio.to_thread(
                _insert_capture_rows,
                "zenplus.host_network_flows",
                rows,
                ["capture_id", "server_id", "agent_id", "observed_at",
                 "protocol", "direction", "kind", "local_ip", "local_port", "remote_ip",
                 "remote_port", "pid", "process_name", "service_name",
                 "state", "bytes_sent", "bytes_received", "bytes_known",
                 "first_seen", "last_seen", "samples"],
            )

    if interfaces:
        interface_rows = [[
            capture_id, str(agent["server_id"]), str(agent["id"]), sample.timestamp,
            sample.interface, sample.interface_index,
            sample.rx_bytes, sample.tx_bytes, sample.rx_bps, sample.tx_bps,
            sample.peak_rx_bps, sample.peak_tx_bps, sample.link_speed_bps,
            sample.receive_link_speed_bps, sample.transmit_link_speed_bps,
            sample.rx_utilization_pct, sample.tx_utilization_pct, now,
        ] for sample in interfaces]
        await asyncio.to_thread(
            _insert_capture_rows,
            "zenplus.host_network_traffic_samples",
            interface_rows,
            ["capture_id", "server_id", "agent_id", "observed_at",
             "interface", "interface_index", "rx_bytes", "tx_bytes",
             "rx_bps", "tx_bps", "peak_rx_bps", "peak_tx_bps",
             "link_speed_bps", "receive_link_speed_bps",
             "transmit_link_speed_bps", "rx_utilization_pct",
             "tx_utilization_pct", "ingested_at"],
        )

    await db.execute(
        text("""UPDATE network_captures SET
                  status = CAST(:st AS VARCHAR),
                  started_at = COALESCE(started_at, CAST(:started AS TIMESTAMPTZ)),
                  ends_at = COALESCE(CAST(:ends AS TIMESTAMPTZ), ends_at),
                  completed_at = CASE WHEN CAST(:st AS VARCHAR) IN ('completed','failed','cancelled')
                                      THEN NOW() ELSE completed_at END,
                  samples = GREATEST(samples, :samples),
                  flow_count = GREATEST(flow_count, :fc),
                  bytes_sent = GREATEST(bytes_sent, :bs),
                  bytes_received = GREATEST(bytes_received, :br),
                  bytes_available = bytes_available OR :ba,
                  truncated = :trunc,
                  note = COALESCE(NULLIF(CAST(:note AS TEXT), ''), note),
                  error_message = COALESCE(NULLIF(CAST(:err AS TEXT), ''), error_message),
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "id": capture_id, "st": effective_status,
            "started": data.started_at,
            "ends": data.ends_at,
            "samples": data.samples,
            "fc": len(flows),
            "bs": sent_total, "br": recv_total,
            "ba": data.bytes_available,
            "trunc": data.truncated,
            "note": data.note or "",
            "err": data.error_message or "",
        },
    )
    await db.commit()
    return {"ok": True, "accepted": len(flows),
            "accepted_interfaces": len(interfaces), "status": effective_status}


def _insert_capture_rows(table: str, rows: list[list[Any]], columns: list[str]) -> None:
    """Run ClickHouse's synchronous insert away from the async API loop."""
    get_clickhouse_client().insert(table, rows, column_names=columns)


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
    await asyncio.to_thread(_verified_package_path, platform, dict(row))
    return {
        "platform": platform,
        "channel": channel,
        "arch": arch,
        "latest_version": row["version"],
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "sha256": row["sha256"],
        "signature": row.get("signature"),
        # Windows updates must be Authenticode-signed even when the package is
        # served from this controller through a relative URL. New agents also
        # fail closed independently, but publishing the requirement keeps the
        # manifest contract explicit for every supported client generation.
        "requires_authenticode": platform == "windows",
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
    file_path = await asyncio.to_thread(_verified_package_path, platform, pkg)
    media = _package_media_type(pkg["file_name"])
    return FileResponse(file_path, media_type=media, filename=pkg["file_name"],
                        headers={"X-Package-Version": pkg["version"],
                                 "X-Package-Sha256": pkg["sha256"]})


_INSTALL_PS1 = r'''#Requires -RunAsAdministrator
<#
ZenPlus agent installer (Windows).
Downloads the latest published package from the controller, verifies its SHA-256,
then installs it with only the controller address and monitoring profile.

Usage:
  .\install.ps1 -ControllerUrl "https://zenplus.example.com" -InstallProfile combined
Optional: -InstallProfile infrastructure|apm|combined  -Arch amd64
#>
param(
    [Parameter(Mandatory=$true)][string]$ControllerUrl,
    [ValidateSet("infrastructure", "apm", "combined")][string]$InstallProfile = "combined",
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

$fileName = [System.IO.Path]::GetFileName($manifest.file_name)
$installer = Join-Path $env:TEMP $fileName
Write-Host "[zenplus] Downloading $fileName ..."
Invoke-WebRequest -UseBasicParsing -Uri "$ControllerUrl/api/v1/agents/packages/windows/latest?arch=$Arch" -OutFile $installer

$actualSha = (Get-FileHash -Algorithm SHA256 -Path $installer).Hash.ToLower()
if ($actualSha -ne $expectedSha) {
    Remove-Item -Force $installer
    throw "[zenplus] SHA-256 mismatch (expected $expectedSha, got $actualSha) - aborting install."
}
Write-Host "[zenplus] Checksum OK."

Write-Host "[zenplus] Installing ..."
$extension = [System.IO.Path]::GetExtension($installer).ToLowerInvariant()
if ($extension -eq ".exe") {
    $installerArgs = @("/machine", "/quiet", "/norestart",
                       "CONTROLLER_URL=`"$ControllerUrl`"",
                       "INSTALL_PROFILE=`"$InstallProfile`"")
    $proc = Start-Process $installer -ArgumentList $installerArgs -Wait -PassThru
} elseif ($extension -eq ".msi") {
    $installerArgs = @("/i", "`"$installer`"", "/quiet", "/norestart",
                       "CONTROLLER_URL=`"$ControllerUrl`"",
                       "INSTALL_PROFILE=`"$InstallProfile`"")
    $proc = Start-Process msiexec.exe -ArgumentList $installerArgs -Wait -PassThru
} else {
    throw "[zenplus] Unsupported Windows installer type: $extension"
}
if ($proc.ExitCode -ne 0) {
    throw "[zenplus] msiexec exited with code $($proc.ExitCode)."
}
Write-Host "[zenplus] Installed. New hosts appear as Pending authorization in Agent Fleet; previously approved hosts resume automatically."
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
    from app.services.network_capture_service import expire_agent_commands
    expired_count = await expire_agent_commands(db, agent_id=agent["id"])
    rows = (await db.execute(
        text("""SELECT id, command, params, expires_at, created_at FROM agent_commands
                WHERE agent_id = :id AND status IN ('queued','sent')
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at LIMIT 10"""),
        {"id": agent["id"]},
    )).mappings().all()
    if not rows:
        if expired_count:
            await db.commit()
        return AgentCommandPoll(has_commands=False, commands=[])
    ids = [r["id"] for r in rows]
    await db.execute(
        text("""UPDATE agent_commands SET status = 'sent', sent_at = COALESCE(sent_at, NOW())
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
        text("SELECT agent_id, command, params FROM agent_commands WHERE id = :id"),
        {"id": command_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Command not found")
    if str(row["agent_id"]) != str(agent["id"]):
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
    from app.services.network_capture_service import reconcile_capture_command_result
    await reconcile_capture_command_result(
        db,
        command=row["command"],
        params=row.get("params") if isinstance(row.get("params"), dict) else {},
        success=data.success,
        error_message=data.error_message,
    )
    from app.services.apm_agent_service import reconcile_apm_command_result
    await reconcile_apm_command_result(
        db,
        agent_id=row["agent_id"],
        command=row["command"],
        params=row.get("params") if isinstance(row.get("params"), dict) else {},
        success=data.success,
        output=data.output or {},
        error_message=data.error_message,
    )
    await db.commit()
    return {"ok": True}
