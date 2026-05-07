"""Sensor-facing API.

These are the endpoints called by the remote sensor binary (or the mock
sensor script). They are intentionally separated from the admin
``/sensors`` router because they:

  * authenticate by per-sensor API key (not by user JWT)
  * accept high-volume batched writes
  * are designed to be safe to expose to remote VMs over WAN

Routes (all prefixed ``/sensor`` -> mounted under ``/api/v1``):

    POST  /sensor/enroll                exchange enrollment token for API key
    POST  /sensor/heartbeat             keep-alive + status
    GET   /sensor/config                pull assigned config (ETag aware)
    POST  /sensor/results/ping          batched ping results
    POST  /sensor/results/service       batched service-check results
    POST  /sensor/results/snmp          batched SNMP results (best-effort)
    POST  /sensor/events                status transitions (no-op v1, logged)
    GET   /sensor/install.sh            tiny installer for the mock sensor

Auth model
----------
The ``/enroll`` endpoint takes an enrollment token in the request body
(one-time, 24h TTL, single-use, hashed at rest).

All other endpoints require:
    Authorization: Bearer <api_key>
    X-Sensor-Id: <uuid>

We hash the api_key with sha256 and compare against ``sensors.api_key_hash``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, get_clickhouse_client
from app.schemas.sensor import (
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    ConfigResponse,
    ConfigDevice,
    ConfigServiceCheck,
    ResultsBatch,
    EventsBatch,
)

router = APIRouter(prefix="/sensor", tags=["Sensor (runtime)"])
logger = logging.getLogger("zenplus.sensor_api")

SENSOR_ARTIFACT_DIR = Path(os.getenv("ZENPLUS_SENSOR_ARTIFACT_DIR", "/opt/zenplus/artifacts/sensors"))
SENSOR_ARTIFACT_BASENAMES = {
    "ova": "zenplus-sensor.ova",
    "ovf": "zenplus-sensor.ovf",
    "sha256": "SHA256SUMS",
    "metadata": "BUILD-METADATA.json",
}


# ── Auth helper ──────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _authenticate(
    sensor_id: str,
    bearer: str,
    db: AsyncSession,
) -> dict:
    """Resolve & validate a sensor by its ID + bearer key.
    Returns the sensor row as a dict on success; raises 401 otherwise.
    """
    if not sensor_id or not bearer:
        raise HTTPException(401, "Missing sensor credentials")

    try:
        sid = UUID(sensor_id)
    except ValueError:
        raise HTTPException(401, "Invalid sensor id")

    row = (await db.execute(
        text("SELECT * FROM sensors WHERE id = :id"),
        {"id": sid},
    )).mappings().first()
    if not row:
        raise HTTPException(401, "Sensor not registered")

    if row["status"] == "disabled":
        raise HTTPException(403, "Sensor disabled")

    expected = row.get("api_key_hash")
    if not expected:
        raise HTTPException(401, "Sensor not enrolled")

    if _sha256(bearer) != expected:
        raise HTTPException(401, "Invalid sensor api key")

    return dict(row)


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


# ── Enroll ───────────────────────────────────────────────────────────

@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    data: EnrollRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time enrollment token for a long-lived API key."""
    if not data.enrollment_token:
        raise HTTPException(400, "enrollment_token required")

    token_hash = _sha256(data.enrollment_token)
    row = (await db.execute(
        text("""SELECT * FROM sensors
                WHERE enrollment_token_hash = :h
                LIMIT 1"""),
        {"h": token_hash},
    )).mappings().first()
    if not row:
        raise HTTPException(401, "Invalid enrollment token")

    if row.get("enrollment_consumed_at"):
        raise HTTPException(401, "Enrollment token already used")

    expires = row.get("enrollment_expires_at")
    if expires:
        # Postgres returns aware datetime
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(401, "Enrollment token expired")

    # Generate API key, write everything in one shot.
    import secrets
    api_key = "zps_key_" + secrets.token_urlsafe(32)
    api_hash = _sha256(api_key)
    prefix = api_key[:12]

    client_ip = _client_ip(request)

    await db.execute(
        text("""UPDATE sensors SET
                  api_key_hash = :h,
                  api_key_prefix = :p,
                  api_key_rotated_at = NOW(),
                  enrollment_consumed_at = NOW(),
                  enrollment_consumed_ip = :ip,
                  hostname = COALESCE(:hostname, hostname),
                  os_info  = COALESCE(:os_info, os_info),
                  version  = COALESCE(:version, version),
                  last_seen_at = NOW(),
                  last_ip = :ip,
                  status = 'online',
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "h": api_hash, "p": prefix,
            "ip": client_ip,
            "hostname": data.hostname, "os_info": data.os_info,
            "version": data.version,
            "id": row["id"],
        },
    )
    await db.commit()
    return EnrollResponse(sensor_id=str(row["id"]), api_key=api_key)


# ── Heartbeat ────────────────────────────────────────────────────────

@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    data: HeartbeatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    client_ip = _client_ip(request)

    await db.execute(
        text("""UPDATE sensors SET
                  last_heartbeat_at = NOW(),
                  last_seen_at = NOW(),
                  last_ip = COALESCE(:ip, last_ip),
                  version = COALESCE(:ver, version),
                  uptime_seconds = COALESCE(:up, uptime_seconds),
                  queue_depth = :qd,
                  queue_dropped_count = :qdd,
                  hostname = COALESCE(:hn, hostname),
                  os_info  = COALESCE(:os, os_info),
                  status = CASE WHEN status = 'disabled' THEN status ELSE 'online' END,
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "ip": client_ip, "ver": data.version, "up": data.uptime_seconds,
            "qd": data.queue_depth, "qdd": data.queue_dropped_count,
            "hn": data.hostname, "os": data.os_info,
            "id": sensor["id"],
        },
    )
    await db.commit()
    etag = await _config_etag(sensor["id"], db)
    return HeartbeatResponse(
        ok=True,
        server_time=datetime.now(timezone.utc),
        config_etag=etag,
        has_commands=False,
    )


# ── Config ───────────────────────────────────────────────────────────

async def _config_etag(sensor_id: UUID, db: AsyncSession) -> str:
    """A cheap ETag: sha256 of the sorted (target_type, target_id, updated_at)
    of every assignment plus the sensor's own updated_at. Stable while
    nothing changes, flips on edits.
    """
    row = (await db.execute(text("SELECT updated_at FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    sensor_ts = row[0].isoformat() if row else ""
    rows = (await db.execute(
        text("""SELECT a.target_type, a.target_id,
                       COALESCE(d.updated_at, sc.updated_at) AS upd
                FROM sensor_assignments a
                LEFT JOIN devices d         ON a.target_type='device' AND d.id = a.target_id
                LEFT JOIN service_checks sc ON a.target_type='service_check' AND sc.id = a.target_id
                WHERE a.sensor_id = :id
                ORDER BY a.target_type, a.target_id"""),
        {"id": sensor_id},
    )).all()
    parts = [sensor_ts]
    for r in rows:
        upd = r[2].isoformat() if r[2] else ""
        parts.append(f"{r[0]}:{r[1]}:{upd}")
    return _sha256("|".join(parts))


@router.get("/config", response_model=ConfigResponse)
async def get_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
    if_none_match: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    etag = await _config_etag(sensor["id"], db)
    if if_none_match and if_none_match.strip('"') == etag:
        raise HTTPException(status_code=304, detail="Not Modified")

    # Pull assigned devices.
    devices = (await db.execute(
        text("""SELECT d.id, d.hostname, host(d.ip_address)::text AS ip_address,
                       d.ping_enabled, d.ping_interval, d.snmp_enabled
                FROM sensor_assignments a
                JOIN devices d ON d.id = a.target_id
                WHERE a.sensor_id = :sid AND a.target_type = 'device'"""),
        {"sid": sensor["id"]},
    )).mappings().all()

    # Pull assigned service checks. Also pull service checks whose
    # default_sensor_id == this sensor, even if no explicit assignment.
    service_checks = (await db.execute(
        text("""SELECT sc.id, sc.name, sc.check_type,
                       sc.target_host, sc.target_port, sc.target_url,
                       sc.http_method, sc.http_expected_statuses, sc.http_content_match,
                       sc.http_follow_redirects,
                       sc.tls_warn_days, sc.tls_critical_days,
                       sc.check_interval, sc.timeout, sc.retry_count, sc.enabled
                FROM service_checks sc
                WHERE sc.id IN (
                    SELECT a.target_id FROM sensor_assignments a
                    WHERE a.sensor_id = :sid AND a.target_type = 'service_check'
                )
                   OR sc.default_sensor_id = :sid"""),
        {"sid": sensor["id"]},
    )).mappings().all()

    return ConfigResponse(
        etag=etag,
        sensor_id=str(sensor["id"]),
        sensor_name=sensor["name"],
        devices=[
            ConfigDevice(
                id=str(d["id"]), hostname=d["hostname"], ip_address=d["ip_address"],
                ping_enabled=bool(d["ping_enabled"]),
                ping_interval=d.get("ping_interval") or 60,
                snmp_enabled=bool(d.get("snmp_enabled") or False),
            )
            for d in devices
        ],
        service_checks=[
            ConfigServiceCheck(
                id=str(sc["id"]), name=sc["name"], check_type=sc["check_type"],
                target_host=sc.get("target_host"), target_port=sc.get("target_port"),
                target_url=sc.get("target_url"),
                http_method=sc.get("http_method"),
                http_expected_statuses=sc.get("http_expected_statuses"),
                http_content_match=sc.get("http_content_match"),
                http_follow_redirects=sc.get("http_follow_redirects"),
                tls_warn_days=sc.get("tls_warn_days"),
                tls_critical_days=sc.get("tls_critical_days"),
                check_interval=sc.get("check_interval") or 60,
                timeout=sc.get("timeout") or 10,
                retry_count=sc.get("retry_count") or 1,
                enabled=bool(sc.get("enabled", True)),
            )
            for sc in service_checks
        ],
    )


# ── Results ingestion ───────────────────────────────────────────────

def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@router.post("/results/ping")
async def post_ping_results(
    body: ResultsBatch,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    if not body.items:
        return {"inserted": 0}

    rows: list[list] = []
    affected_device_ids: set[str] = set()
    last_status: dict[str, dict] = {}

    for it in body.items:
        if not isinstance(it, dict):
            it = it.model_dump() if hasattr(it, "model_dump") else dict(it)
        device_id = str(it.get("device_id"))
        affected_device_ids.add(device_id)
        ts = _parse_dt(it.get("timestamp"))
        is_up = 1 if it.get("is_up") else 0
        rtt = float(it.get("rtt_ms") or 0.0)
        ip = it.get("ip_address") or "0.0.0.0"
        rows.append([
            device_id, ts, is_up, rtt,
            0.0,        # packet_loss (placeholder)
            0.0, rtt, rtt,   # jitter, min, max
            1, 1 if is_up else 0,  # packets sent/recv
            str(sensor["id"]),
            ip,
        ])
        last_status[device_id] = {"is_up": is_up, "rtt": rtt, "ts": ts, "ip": ip}

    client = get_clickhouse_client()
    client.insert(
        "ping_metrics",
        rows,
        column_names=[
            "device_id", "timestamp", "is_up", "rtt_ms",
            "packet_loss", "jitter_ms", "min_rtt_ms", "max_rtt_ms",
            "packets_sent", "packets_recv", "poller_id", "ip_address",
        ],
    )

    # Update each device's status / last_seen / last_rtt_ms in Postgres.
    for did, s in last_status.items():
        try:
            new_status = "up" if s["is_up"] else "down"
            await db.execute(
                text("""UPDATE devices SET
                          status = :st,
                          last_seen = :ts,
                          last_rtt_ms = :rtt,
                          updated_at = NOW()
                        WHERE id = :id"""),
                {"st": new_status, "ts": s["ts"], "rtt": s["rtt"], "id": did},
            )
        except Exception as e:
            logger.warning("device update failed for %s: %s", did, e)
    await db.commit()

    return {"inserted": len(rows)}


@router.post("/results/service")
async def post_service_results(
    body: ResultsBatch,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    if not body.items:
        return {"inserted": 0}

    rows: list[list] = []
    last_status: dict[str, dict] = {}

    for it in body.items:
        if not isinstance(it, dict):
            it = it.model_dump() if hasattr(it, "model_dump") else dict(it)
        sc_id = str(it.get("service_check_id"))
        ts = _parse_dt(it.get("timestamp"))
        check_type = it.get("check_type") or "http"
        is_up = 1 if it.get("is_up") else 0
        resp_ms = float(it.get("response_ms") or 0.0)
        status_code = it.get("status_code")
        err = it.get("error") or ""
        rows.append([
            sc_id,
            "00000000-0000-0000-0000-000000000000",  # device_id placeholder; checks may not be tied to a device
            ts, check_type, is_up, resp_ms,
            int(status_code) if status_code is not None else None,
            None,           # tls_days_remaining
            None,           # tls_valid
            None,           # content_matched
            err if err else None,
            str(sensor["id"]),
        ])
        last_status[sc_id] = {"is_up": is_up, "ts": ts}

    client = get_clickhouse_client()
    client.insert(
        "service_metrics",
        rows,
        column_names=[
            "service_check_id", "device_id", "timestamp", "check_type",
            "is_up", "response_ms", "status_code",
            "tls_days_remaining", "tls_valid", "content_matched",
            "error_message", "poller_id",
        ],
    )

    for sc_id, s in last_status.items():
        try:
            await db.execute(
                text("""UPDATE service_checks SET
                          status = :st,
                          last_check_at = :ts,
                          updated_at = NOW()
                        WHERE id = :id"""),
                {"st": "up" if s["is_up"] else "down", "ts": s["ts"], "id": sc_id},
            )
        except Exception as e:
            # Some columns may not exist in older schemas — degrade silently.
            logger.debug("service_check status update skipped for %s: %s", sc_id, e)
    await db.commit()

    return {"inserted": len(rows)}


@router.post("/results/snmp")
async def post_snmp_results(
    body: ResultsBatch,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """Best-effort SNMP ingest. v1 simply acknowledges and logs a count;
    full SNMP storage path is deferred until the Go remote-mode lands.
    """
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    n = len(body.items or [])
    logger.info("sensor %s snmp batch: %d items", sensor["id"], n)
    return {"accepted": n}


# ── Events ───────────────────────────────────────────────────────────

@router.post("/events")
async def post_events(
    body: EventsBatch,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    n = len(body.items or [])
    logger.info("sensor %s events: %d", sensor["id"], n)
    return {"accepted": n}


# ── Sensor appliance downloads ───────────────────────────────────────

def _artifact_path(kind: str) -> Path:
    filename = SENSOR_ARTIFACT_BASENAMES.get(kind)
    if not filename:
        raise HTTPException(404, "Unknown sensor appliance artifact")
    return SENSOR_ARTIFACT_DIR / filename


def _artifact_info(kind: str, request: Request) -> dict:
    path = _artifact_path(kind)
    url = str(request.url_for("download_sensor_appliance_artifact", kind=kind))
    available = path.exists() and path.is_file()
    sha_file = SENSOR_ARTIFACT_DIR / "SHA256SUMS"
    sha256 = None
    if available and sha_file.exists():
        try:
            for line in sha_file.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path.name:
                    sha256 = parts[0]
                    break
        except Exception:
            sha256 = None
    return {
        "kind": kind,
        "filename": path.name,
        "available": available,
        "url": url,
        "size_bytes": path.stat().st_size if available else None,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) if available else None,
        "sha256": sha256,
    }


def _appliance_metadata() -> dict[str, Any]:
    path = _artifact_path("metadata")
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("invalid sensor appliance metadata %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/appliance/manifest")
async def sensor_appliance_manifest(request: Request):
    """Return controller-hosted sensor appliance artifact metadata.

    Build/publish scripts place artifacts under SENSOR_ARTIFACT_DIR. The
    controller can expose links before artifacts exist so the dashboard flow is
    stable while release engineering wires the OVA/OVF pipeline.
    """
    has_appliance = _artifact_path("ova").exists() or _artifact_path("ovf").exists()
    metadata = _appliance_metadata()
    is_bootable = metadata.get("type") == "bootable-ova"
    status = "ready" if has_appliance and is_bootable else "preview" if has_appliance else "not_published"
    note = None
    if status == "preview":
        note = (
            "Preview artifacts are sufficient for controller download/onboarding tests. "
            "Build and publish the real OVA before deploying a production sensor VM."
        )
    elif status == "ready":
        note = "Bootable cloud-init-ready OVA is published and available for sensor deployment."
    return {
        "product": "ZenPlus Remote Sensor",
        "status": status,
        "note": note,
        "metadata": metadata,
        "artifact_dir": str(SENSOR_ARTIFACT_DIR),
        "artifacts": [
            _artifact_info("ova", request),
            _artifact_info("ovf", request),
            _artifact_info("sha256", request),
        ],
        "bootstrap": {
            "method": "cloud-init NoCloud seed or first-boot wizard",
            "required_values": ["server_url", "enrollment_token", "sensor_name"],
        },
    }


@router.get("/appliance/{kind}", name="download_sensor_appliance_artifact")
async def download_sensor_appliance_artifact(kind: str):
    path = _artifact_path(kind)
    if not path.exists() or not path.is_file():
        raise HTTPException(
            404,
            f"Sensor appliance artifact '{path.name}' is not published yet. "
            "Run sensor-appliance/scripts/publish-artifacts.sh after building the OVA/OVF.",
        )

    media_types = {
        "ova": "application/x-virtualbox-ova",
        "ovf": "application/xml",
        "sha256": "text/plain",
    }
    return FileResponse(path, media_type=media_types.get(kind, "application/octet-stream"), filename=path.name)


# ── Tiny installer for the mock sensor (Phase 1 only) ────────────────

INSTALL_SH = """\
#!/usr/bin/env bash
# ZenPlus mock sensor installer (Phase 1 / testing only).
#
# Required env:
#   ZENPLUS_SERVER_URL=https://central
#   ZENPLUS_ENROLLMENT_TOKEN=zps_enr_...
#   ZENPLUS_SENSOR_NAME=branch-name
set -euo pipefail
: "${ZENPLUS_SERVER_URL:?set ZENPLUS_SERVER_URL}"
: "${ZENPLUS_ENROLLMENT_TOKEN:?set ZENPLUS_ENROLLMENT_TOKEN}"
: "${ZENPLUS_SENSOR_NAME:?set ZENPLUS_SENSOR_NAME}"

INSTALL_DIR="${ZENPLUS_INSTALL_DIR:-$HOME/zenplus-sensor}"
mkdir -p "$INSTALL_DIR"

echo "Fetching mock sensor script…"
curl -sSL "$ZENPLUS_SERVER_URL/api/v1/sensor/mock_sensor.py" -o "$INSTALL_DIR/mock_sensor.py"
chmod +x "$INSTALL_DIR/mock_sensor.py"

echo
echo "Run:"
echo "  ZENPLUS_SERVER_URL='$ZENPLUS_SERVER_URL' \\\\"
echo "  ZENPLUS_ENROLLMENT_TOKEN='$ZENPLUS_ENROLLMENT_TOKEN' \\\\"
echo "  ZENPLUS_SENSOR_NAME='$ZENPLUS_SENSOR_NAME' \\\\"
echo "  python3 $INSTALL_DIR/mock_sensor.py"
"""


@router.get("/install.sh", response_class=PlainTextResponse)
async def install_sh():
    return INSTALL_SH


@router.get("/mock_sensor.py", response_class=PlainTextResponse)
async def mock_sensor_py():
    """Serve the mock sensor python script.

    Lives in /opt/zenplus/scripts/mock_sensor.py in production. Falls
    back to the repo path while developing.
    """
    import os
    candidates = [
        "/opt/zenplus/scripts/mock_sensor.py",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts", "mock_sensor.py"),
    ]
    for p in candidates:
        try:
            with open(os.path.abspath(p), "r") as f:
                return f.read()
        except FileNotFoundError:
            continue
    raise HTTPException(404, "mock_sensor.py not deployed")
