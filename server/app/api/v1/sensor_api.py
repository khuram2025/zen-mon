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
    GET   /sensor/install.sh            Ubuntu one-line sensor installer

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

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db, get_clickhouse_client
from app.services.sensor_health_service import create_sensor_alert, resolve_sensor_alert
from app.services.sensor_rate_limit import enforce_sensor_quota
from app.services.audit_service import write_audit_log
from app.schemas.sensor import (
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    ConfigResponse,
    ConfigDevice,
    ConfigServiceCheck,
    ResultsBatch,
    PingResultsBatch,
    ServiceResultsBatch,
    SnmpResultsBatch,
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
    "qcow2": "zenplus-sensor.qcow2",
    "vhdx": "zenplus-sensor.vhdx",
}
SENSOR_BINARY_NAME = "zenplus-sensor"
SENSOR_BINARY_SHA_NAME = "zenplus-sensor.sha256"
SENSOR_RELEASE_PUBLIC_KEY = Path(
    os.getenv(
        "ZENPLUS_SENSOR_RELEASE_PUBLIC_KEY",
        os.getenv("ZENPLUS_RELEASE_PUBLIC_KEY", "/opt/zenplus/updater/keys/zentryc-release.pub"),
    )
)
SUPPORTED_SENSOR_BINARY_PLATFORMS = {
    "linux-amd64": ("linux", "amd64"),
}
SENSOR_BACKFILL_WINDOW = timedelta(hours=72)
SENSOR_FUTURE_SKEW = timedelta(seconds=60)
MIN_SUPPORTED_SENSOR_VERSION = os.getenv("ZENPLUS_MIN_SENSOR_VERSION", "").strip() or None


# ── Auth helper ──────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _authenticate(
    sensor_id: str,
    bearer: str,
    db: AsyncSession,
    allow_pending: bool = False,
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

    if not hmac.compare_digest(_sha256(bearer), str(expected)):
        raise HTTPException(401, "Invalid sensor api key")

    if not allow_pending and (row.get("bootstrap_config") or {}).get("authorization_pending"):
        raise HTTPException(403, "Sensor disabled: awaiting administrator authorization")

    return dict(row)


async def _lock_live_sensor_for_results(sensor: dict, db: AsyncSession) -> None:
    # A shared row lock serializes the whole result transaction with the health
    # sweep's FOR UPDATE transition. If the sweep wins, we see offline and keep
    # the batch in WAL. If ingest wins, a later offline sweep resets live state
    # to unknown after this transaction commits, preserving the next alert edge.
    status = (await db.execute(
        text("SELECT status FROM sensors WHERE id = :id FOR SHARE"),
        {"id": sensor["id"]},
    )).scalar_one()
    if status == "offline":
        # Keep the batch in the sensor WAL until its control-plane heartbeat
        # restores the vantage. Otherwise a down transition can be suppressed
        # while the owner is offline and never re-emitted after recovery.
        raise HTTPException(
            409,
            "Sensor is marked offline; send a successful heartbeat before uploading results",
        )


def _strip_bearer(value: Optional[str]) -> str:
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # nginx appends the immediate client to this chain; the last hop is
        # therefore the spoof-resistant address for the local reverse proxy.
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else None


async def _limit_sensor_request(
    request: Request,
    scope: str,
    *,
    sensor_id: Optional[str] = None,
    amount: int = 1,
    limit: int = 300,
) -> None:
    ip_key = _sha256(_client_ip(request) or "unknown")[:24]
    await enforce_sensor_quota(f"{scope}:ip", ip_key, amount=amount, limit=limit)
    if sensor_id:
        await enforce_sensor_quota(
            f"{scope}:sensor", sensor_id, amount=amount, limit=limit
        )


def _binary_dir(platform: str) -> Path:
    if platform not in SUPPORTED_SENSOR_BINARY_PLATFORMS:
        raise HTTPException(404, "Unsupported sensor binary platform")
    return SENSOR_ARTIFACT_DIR / "bin" / platform


def _binary_path(platform: str) -> Path:
    return _binary_dir(platform) / SENSOR_BINARY_NAME


def _binary_sha_path(platform: str) -> Path:
    return _binary_dir(platform) / SENSOR_BINARY_SHA_NAME


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _binary_sha_line(platform: str) -> str:
    binary = _binary_path(platform)
    if not binary.exists() or not binary.is_file():
        raise HTTPException(
            404,
            f"Sensor binary for {platform} is not published yet. "
            "Build the appliance release or run the installer build step first.",
        )
    sha_path = _binary_sha_path(platform)
    if sha_path.exists() and sha_path.is_file():
        text_value = sha_path.read_text().strip()
        if text_value:
            return text_value + "\n"
    return f"{_sha256_path(binary)}  {SENSOR_BINARY_NAME}\n"


def _binary_metadata(platform: str) -> dict[str, Any]:
    path = _binary_dir(platform) / "manifest.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _signed_binary_metadata(
    platform: str,
) -> tuple[dict[str, Any], Optional[bytes], Optional[bytes]]:
    """Return a verified offline-signed sensor manifest, or an empty result.

    The same Ed25519 release key that protects appliance OTA signs this static
    manifest during release packaging. The API never manufactures a signature
    and unsigned git/local builds remain downloadable for initial install but
    are ineligible for remote self-update.
    """
    directory = _binary_dir(platform)
    manifest_path = directory / "manifest.json"
    signature_path = directory / "manifest.json.sig"
    try:
        manifest_data = manifest_path.read_bytes()
        signature = signature_path.read_bytes()
        public_key = load_pem_public_key(SENSOR_RELEASE_PUBLIC_KEY.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey) or len(signature) != 64:
            return {}, None, None
        public_key.verify(signature, manifest_data)
        metadata = json.loads(manifest_data)
    except (OSError, TypeError, ValueError, InvalidSignature):
        return {}, None, None
    if not isinstance(metadata, dict):
        return {}, None, None
    os_name, arch = SUPPORTED_SENSOR_BINARY_PLATFORMS[platform]
    if (
        metadata.get("platform") != platform
        or metadata.get("os") != os_name
        or metadata.get("arch") != arch
        or metadata.get("binary") != SENSOR_BINARY_NAME
        or metadata.get("binary_url") != SENSOR_BINARY_NAME
        or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256") or ""))
        or not str(metadata.get("version") or "").strip()
    ):
        return {}, None, None
    return metadata, manifest_data, signature


def _public_route_url(request: Request, route_name: str, **params: str) -> str:
    route = request.url_for(route_name, **params)
    configured = (get_settings().APP_BASE_URL or "").strip().rstrip("/")
    return f"{configured}{route.path}" if configured else str(route)


def _controller_ca_sha256() -> Optional[str]:
    configured = os.getenv("ZENPLUS_CONTROLLER_CA_SHA256", "").strip().lower()
    if configured:
        if re.fullmatch(r"[0-9a-f]{64}", configured):
            return configured
        logger.error("ZENPLUS_CONTROLLER_CA_SHA256 must be exactly 64 hexadecimal characters")
        return None

    path = Path(os.getenv("ZENPLUS_CONTROLLER_CA_CERT", "/etc/zenplus/tls/fullchain.pem"))
    try:
        pem = path.read_text()
        blocks = re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            pem,
            flags=re.DOTALL,
        )
        if not blocks:
            return None
        # Only auto-pin a self-signed trust anchor actually present in the
        # configured bundle (the appliance's default self-signed certificate,
        # or an explicitly bundled root). Public leaf/intermediate chains use
        # normal TLS trust validation so routine leaf renewal cannot strand the
        # fleet. Operators may pin a private root explicitly with the env var.
        certificate = x509.load_pem_x509_certificate(blocks[-1].encode("ascii"))
        if certificate.subject != certificate.issuer:
            return None
        der = certificate.public_bytes(Encoding.DER)
    except (OSError, ValueError):
        return None
    return hashlib.sha256(der).hexdigest()


# ── Enroll ───────────────────────────────────────────────────────────

@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    data: EnrollRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time enrollment token for a long-lived API key."""
    await _limit_sensor_request(request, "enroll", limit=20)
    if not data.enrollment_token:
        raise HTTPException(400, "enrollment_token required")

    token_hash = _sha256(data.enrollment_token)
    row = (await db.execute(
        text("""SELECT * FROM sensors
                WHERE enrollment_token_hash = :h
                LIMIT 1
                FOR UPDATE"""),
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
                  last_heartbeat_at = NOW(),
                  last_ip = :ip,
                  status = CASE WHEN COALESCE((bootstrap_config->>'authorization_pending')::boolean, false)
                                THEN 'pending' ELSE 'online' END,
                  status_reason = NULL,
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
    await db.execute(
        text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                VALUES (:id, 'enrolled', CAST(:detail AS jsonb))"""),
        {
            "id": row["id"],
            "detail": json.dumps({
                "hostname": data.hostname,
                "version": data.version,
                "source_ip": client_ip,
            }),
        },
    )
    await write_audit_log(
        db,
        actor=None,
        action="sensor.enroll",
        resource_type="sensor",
        resource_id=str(row["id"]),
        metadata={"hostname": data.hostname, "version": data.version, "source_ip": client_ip},
    )
    await db.commit()
    try:
        from app.api.v1.sensors import purge_sensor_bootstrap_artifacts
        await asyncio.to_thread(purge_sensor_bootstrap_artifacts, row["id"])
    except Exception as exc:
        logger.warning("failed to purge consumed bootstrap artifacts for %s: %s", row["id"], exc)
    return EnrollResponse(
        sensor_id=str(row["id"]),
        api_key=api_key,
        controller_ca_sha256=_controller_ca_sha256(),
    )


# ── Heartbeat ────────────────────────────────────────────────────────

def _version_parts(value: Optional[str]) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value or "")[:3])


def _heartbeat_health(sensor: dict, data: HeartbeatRequest) -> tuple[str, Optional[str]]:
    previous_drops = int(sensor.get("queue_dropped_count") or 0)
    if data.queue_dropped_count > previous_drops:
        return "degraded", "Sensor buffer overflowed; probe results were dropped"

    version = data.version or sensor.get("version")
    minimum = sensor.get("min_supported_version")
    current_parts = _version_parts(version)
    minimum_parts = _version_parts(minimum)
    if minimum_parts and (not current_parts or current_parts < minimum_parts):
        return "degraded", f"Sensor version {version or 'unknown'} is below required {minimum}"
    return "online", None


async def _heartbeat_commands(
    sensor: dict,
    data: HeartbeatRequest,
    request: Request,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Queue automatic upgrades and lease active commands to the sensor.

    Delivery is intentionally at-least-once: a command remains visible until a
    matching durable command outcome arrives through ``/sensor/events``. The
    runtime keeps a bounded completed-command cache, so a lost outcome upload
    cannot execute a command twice.
    """
    await db.execute(
        text("""UPDATE sensor_commands
                   SET status = 'expired', completed_at = NOW(),
                       result = COALESCE(result, 'command expired before completion')
                 WHERE sensor_id = :sensor_id
                   AND status IN ('pending', 'delivered')
                   AND expires_at <= NOW()"""),
        {"sensor_id": sensor["id"]},
    )

    minimum = MIN_SUPPORTED_SENSOR_VERSION or sensor.get("min_supported_version")
    current = data.version or sensor.get("version")
    metadata, signed_manifest, signature = _signed_binary_metadata("linux-amd64")
    target = str(metadata.get("version") or "").strip()
    binary_available = _binary_path("linux-amd64").is_file()
    manifest_url = _public_route_url(
        request, "sensor_binary_manifest", platform="linux-amd64"
    )
    if (
        minimum
        and target
        and binary_available
        and signed_manifest is not None
        and signature is not None
        and manifest_url.lower().startswith("https://")
        and _version_parts(current) < _version_parts(minimum)
        and _version_parts(target) >= _version_parts(minimum)
    ):
        payload = {
            "manifest_url": manifest_url,
            "version": target,
        }
        await db.execute(
            text("""INSERT INTO sensor_commands (
                        sensor_id, verb, payload, expires_at
                    )
                    SELECT :sensor_id, 'update', CAST(:payload AS jsonb),
                           NOW() + INTERVAL '24 hours'
                     WHERE NOT EXISTS (
                        SELECT 1 FROM sensor_commands
                         WHERE sensor_id = :sensor_id AND verb = 'update'
                           AND (
                                status IN ('pending', 'delivered')
                                OR (status = 'failed' AND created_at > NOW() - INTERVAL '6 hours')
                           )
                    )
                    ON CONFLICT DO NOTHING"""),
            {"sensor_id": sensor["id"], "payload": json.dumps(payload)},
        )

    rows = (await db.execute(
        text("""SELECT id, verb, payload
                  FROM sensor_commands
                 WHERE sensor_id = :sensor_id
                   AND status IN ('pending', 'delivered')
                   AND expires_at > NOW()
                 ORDER BY created_at, id
                 LIMIT 10
                 FOR UPDATE"""),
        {"sensor_id": sensor["id"]},
    )).mappings().all()
    if rows:
        await db.execute(
            text("""UPDATE sensor_commands
                       SET status = 'delivered',
                           delivery_count = delivery_count + 1,
                           last_delivered_at = NOW()
                     WHERE id = :id"""),
            [{"id": row["id"]} for row in rows],
        )
    return [
        {"id": str(row["id"]), "verb": row["verb"], "payload": row["payload"] or {}}
        for row in rows
    ]

@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    data: HeartbeatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db, allow_pending=True)
    await _limit_sensor_request(request, "heartbeat", sensor_id=x_sensor_id, limit=180)
    if (sensor.get("bootstrap_config") or {}).get("authorization_pending"):
        await db.execute(text("""UPDATE sensors SET last_heartbeat_at = NOW(), last_seen_at = NOW(),
            last_ip = :ip, version = :version, hostname = :hostname,
            status = 'pending', status_reason = 'Awaiting administrator authorization'
            WHERE id = :id"""), {"id": sensor["id"], "ip": _client_ip(request), "version": data.version, "hostname": data.hostname})
        await db.commit()
        raise HTTPException(403, "Sensor disabled: awaiting administrator authorization")
    client_ip = _client_ip(request)
    if MIN_SUPPORTED_SENSOR_VERSION:
        sensor["min_supported_version"] = MIN_SUPPORTED_SENSOR_VERSION
    next_status, status_reason = _heartbeat_health(sensor, data)

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
                  min_supported_version = COALESCE(:minimum_version, min_supported_version),
                  status = :status,
                  status_reason = :reason,
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "ip": client_ip, "ver": data.version, "up": data.uptime_seconds,
            "qd": data.queue_depth, "qdd": data.queue_dropped_count,
            "hn": data.hostname, "os": data.os_info,
            "status": next_status, "reason": status_reason,
            "minimum_version": MIN_SUPPORTED_SENSOR_VERSION,
            "id": sensor["id"],
        },
    )
    if sensor["status"] != next_status or sensor.get("status_reason") != status_reason:
        await db.execute(
            text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                    VALUES (:id, 'status_changed', CAST(:detail AS jsonb))"""),
            {
                "id": sensor["id"],
                "detail": json.dumps({
                    "from": sensor["status"],
                    "to": next_status,
                    "reason": status_reason or "heartbeat resumed",
                }),
            },
        )
    await resolve_sensor_alert(
        db, str(sensor["id"]), "sensor_offline", "heartbeat resumed"
    )
    if next_status == "online":
        await resolve_sensor_alert(
            db, str(sensor["id"]), "sensor_degraded", "sensor health recovered"
        )
    else:
        await create_sensor_alert(
            db,
            sensor_id=str(sensor["id"]),
            severity="warning",
            message=f"Remote sensor {sensor['name']} is degraded — {status_reason}",
            source="sensor_degraded",
        )
    commands = await _heartbeat_commands(sensor, data, request, db)
    await db.commit()
    etag = await _config_etag(sensor["id"], db)
    return HeartbeatResponse(
        ok=True,
        server_time=datetime.now(timezone.utc),
        config_etag=etag,
        has_commands=bool(commands),
        min_supported_version=MIN_SUPPORTED_SENSOR_VERSION or sensor.get("min_supported_version"),
        commands=commands,
    )


# ── Config ───────────────────────────────────────────────────────────

async def _config_etag(sensor_id: UUID, db: AsyncSession) -> str:
    """Hash the effective probe configuration, never telemetry timestamps."""
    sensor = (await db.execute(
        text("SELECT name, site_id, location FROM sensors WHERE id = :id"),
        {"id": sensor_id},
    )).mappings().first()
    assignments = (await db.execute(
        text("""SELECT target_type, target_id::text AS target_id, priority
                  FROM sensor_assignments
                 WHERE sensor_id = :id
                 ORDER BY target_type, target_id, priority"""),
        {"id": sensor_id},
    )).mappings().all()
    devices = (await db.execute(
        text("""SELECT d.id::text AS id, d.hostname,
                       host(d.ip_address)::text AS ip_address,
                       d.ping_enabled, d.ping_interval, d.snmp_enabled
                  FROM devices d
                  JOIN device_monitoring_vantages owner ON owner.device_id = d.id
                 WHERE owner.sensor_id = :id
                 ORDER BY d.id"""),
        {"id": sensor_id},
    )).mappings().all()
    service_checks = (await db.execute(
        text("""SELECT sc.id::text AS id, sc.name, sc.check_type,
                       sc.target_host, sc.target_port, sc.target_url,
                       sc.http_method, sc.http_headers, sc.http_body,
                       sc.http_expected_status, sc.http_expected_statuses,
                       sc.http_content_match, sc.http_follow_redirects,
                       sc.http_ignore_tls_errors, sc.http_allow_insecure_auth,
                       sc.config, sc.tls_warn_days,
                       sc.tls_critical_days, sc.check_interval, sc.timeout,
                       sc.retry_count, COALESCE(sc.retry_delay_s, 30) AS retry_delay_s,
                       sc.enabled, sc.credential_id, sc.workflow_operator, sc.workflow_steps,
                       cred.auth_type AS credential_auth_type, cred.username AS credential_username, cred.secret_cipher
                  FROM service_checks sc LEFT JOIN service_credentials cred ON cred.id=sc.credential_id
                 WHERE EXISTS (SELECT 1 FROM service_monitoring_vantages v WHERE v.service_check_id=sc.id AND v.sensor_id=:id)
                 ORDER BY sc.id"""),
        {"id": sensor_id},
    )).mappings().all()
    from app.services.sensor_snmp import sensor_snmp_config
    snmp = await sensor_snmp_config(sensor_id, db)
    material = {
        "snmp": {key: value.model_dump() for key, value in snmp.items()},
        "sensor": dict(sensor) if sensor else None,
        "assignments": [dict(row) for row in assignments],
        "devices": [dict(row) for row in devices],
        "service_checks": [dict(row) for row in service_checks],
    }
    return _sha256(json.dumps(material, default=str, sort_keys=True, separators=(",", ":")))


@router.get("/config", response_model=ConfigResponse)
async def get_config(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
    if_none_match: str = Header(default=""),
):
    if request.url.scheme != "https":
        raise HTTPException(400, "Sensor configuration requires HTTPS")
    response.headers["Cache-Control"] = "no-store"
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    await _limit_sensor_request(request, "config", sensor_id=x_sensor_id, limit=120)
    etag = await _config_etag(sensor["id"], db)
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304, headers={"Cache-Control": "no-store"})

    # Pull assigned devices.
    devices = (await db.execute(
        text("""SELECT DISTINCT d.id, d.hostname, host(d.ip_address)::text AS ip_address,
                       d.ping_enabled, d.ping_interval, d.snmp_enabled
                FROM devices d
                JOIN device_monitoring_vantages owner ON owner.device_id = d.id
                WHERE owner.sensor_id = :sid
                ORDER BY d.hostname"""),
        {"sid": sensor["id"]},
    )).mappings().all()

    # Pull assigned service checks. Also pull service checks whose
    # default_sensor_id == this sensor, even if no explicit assignment.
    service_checks = (await db.execute(
        text("""SELECT sc.id, sc.name, sc.check_type,
                       sc.target_host, sc.target_port, sc.target_url,
                       sc.http_method, sc.http_headers, sc.http_body,
                       sc.http_expected_status, sc.http_expected_statuses,
                       sc.http_content_match, sc.http_follow_redirects,
                       sc.http_ignore_tls_errors, sc.http_allow_insecure_auth,
                       sc.config,
                       sc.tls_warn_days, sc.tls_critical_days,
                       sc.check_interval, sc.timeout, sc.retry_count,
                       COALESCE(sc.retry_delay_s, 30) AS retry_delay_s, sc.enabled, sc.credential_id, sc.workflow_operator, sc.workflow_steps,
                       cred.auth_type AS credential_auth_type, cred.username AS credential_username, cred.secret_cipher
                FROM service_checks sc LEFT JOIN service_credentials cred ON cred.id=sc.credential_id
                WHERE sc.enabled = TRUE
                  AND EXISTS (SELECT 1 FROM service_monitoring_vantages v WHERE v.service_check_id=sc.id AND v.sensor_id=:sid)
                ORDER BY sc.name"""),
        {"sid": sensor["id"]},
    )).mappings().all()

    from app.services.sensor_snmp import sensor_snmp_config
    from app.services.sensor_service_checks import service_auth_config
    snmp = await sensor_snmp_config(sensor["id"], db)
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
                snmp=snmp.get(str(d["id"])),
            )
            for d in devices
        ],
        service_checks=[
            ConfigServiceCheck(
                id=str(sc["id"]), name=sc["name"], check_type=sc["check_type"],
                **service_auth_config(sc),
                target_host=sc.get("target_host"), target_port=sc.get("target_port"),
                target_url=sc.get("target_url"),
                http_method=sc.get("http_method"),
                http_headers={
                    str(key): str(value)
                    for key, value in dict(sc.get("http_headers") or {}).items()
                },
                http_body=sc.get("http_body"),
                http_expected_status=sc.get("http_expected_status") or 200,
                http_expected_statuses=sc.get("http_expected_statuses"),
                http_content_match=sc.get("http_content_match"),
                http_follow_redirects=sc.get("http_follow_redirects"),
                http_ignore_tls_errors=bool(sc.get("http_ignore_tls_errors", False)),
                http_allow_insecure_auth=bool(sc.get("http_allow_insecure_auth", False)),
                config=dict(sc.get("config") or {}),
                tls_warn_days=sc.get("tls_warn_days"),
                tls_critical_days=sc.get("tls_critical_days"),
                check_interval=sc.get("check_interval") or 60,
                timeout=sc.get("timeout") or 10,
                retry_count=sc.get("retry_count") or 1,
                retry_delay_s=sc.get("retry_delay_s") or 30,
                enabled=bool(sc.get("enabled", True)),
            )
            for sc in service_checks
        ],
    )


# ── Results ingestion ───────────────────────────────────────────────

async def _allowed_device_ids(sensor_id: UUID, device_ids: set[UUID], db: AsyncSession) -> set[str]:
    if not device_ids:
        return set()
    rows = (await db.execute(
        text("""SELECT owner.device_id::text
                  FROM device_monitoring_vantages owner
                 WHERE owner.device_id = ANY(:ids) AND owner.sensor_id = :sid"""),
        {"sid": sensor_id, "ids": list(device_ids)},
    )).all()
    return {str(r[0]) for r in rows}


async def _allowed_service_check_ids(sensor_id: UUID, check_ids: set[UUID], db: AsyncSession) -> set[str]:
    if not check_ids:
        return set()
    rows = (await db.execute(
        text("""SELECT sc.id::text
                FROM service_checks sc
                WHERE sc.id = ANY(:ids)
                  AND EXISTS (SELECT 1 FROM service_monitoring_vantages v WHERE v.service_check_id=sc.id AND v.sensor_id=:sid)"""),
        {"sid": sensor_id, "ids": list(check_ids)},
    )).all()
    return {str(r[0]) for r in rows}


def _uuid_from_item(item: dict, key: str) -> UUID:
    raw = item.get(key)
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid {key}")


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise HTTPException(400, "Every result requires a valid ISO-8601 timestamp")


def _batch_payload_sha256(body: ResultsBatch) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json")["items"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def _reserve_ingest(
    sensor_id: UUID,
    endpoint: str,
    body: ResultsBatch,
    db: AsyncSession,
) -> Optional[dict[str, Any]]:
    """Reserve an idempotent batch or return its prior completed outcome."""
    if not body.idempotency_key:
        raise HTTPException(400, "idempotency_key is required")
    payload_sha = _batch_payload_sha256(body)
    inserted = (await db.execute(
        text("""INSERT INTO sensor_ingest_ledger (
                    sensor_id, endpoint, idempotency_key, payload_sha256
                ) VALUES (:sid, :endpoint, :key, :sha)
                ON CONFLICT (sensor_id, endpoint, idempotency_key) DO NOTHING
                RETURNING idempotency_key"""),
        {
            "sid": sensor_id,
            "endpoint": endpoint,
            "key": body.idempotency_key,
            "sha": payload_sha,
        },
    )).first()
    if inserted:
        return None

    prior = (await db.execute(
        text("""SELECT payload_sha256, accepted, dropped, completed_at
                  FROM sensor_ingest_ledger
                 WHERE sensor_id = :sid AND endpoint = :endpoint
                   AND idempotency_key = :key"""),
        {"sid": sensor_id, "endpoint": endpoint, "key": body.idempotency_key},
    )).mappings().first()
    if not prior:
        raise HTTPException(409, "Batch reservation changed concurrently; retry")
    if not hmac.compare_digest(str(prior["payload_sha256"]), payload_sha):
        raise HTTPException(409, "idempotency_key was already used for a different payload")
    if prior.get("completed_at") is None:
        raise HTTPException(409, "Batch with this idempotency_key is still processing")
    return {
        "inserted": int(prior.get("accepted") or 0),
        "dropped": int(prior.get("dropped") or 0),
        "duplicate": True,
    }


async def _complete_ingest(
    sensor_id: UUID,
    endpoint: str,
    key: str,
    accepted: int,
    dropped: int,
    db: AsyncSession,
) -> None:
    await db.execute(
        text("""UPDATE sensor_ingest_ledger
                   SET accepted = :accepted, dropped = :dropped, completed_at = NOW()
                 WHERE sensor_id = :sid AND endpoint = :endpoint
                   AND idempotency_key = :key"""),
        {
            "sid": sensor_id,
            "endpoint": endpoint,
            "key": key,
            "accepted": accepted,
            "dropped": dropped,
        },
    )


async def _queue_transitions(
    sensor_id: UUID,
    endpoint: str,
    idempotency_key: str,
    transition_type: str,
    transitions: list[dict[str, Any]],
    db: AsyncSession,
) -> None:
    """Persist alert/log work in the same transaction as live status."""
    if not transitions:
        return
    entity_key = "device_id" if transition_type == "device" else "service_check_id"
    await db.execute(
        text("""INSERT INTO sensor_transition_outbox (
                    sensor_id, endpoint, idempotency_key, transition_type,
                    entity_id, payload
                ) VALUES (
                    :sensor_id, :endpoint, :idempotency_key, :transition_type,
                    :entity_id, CAST(:payload AS jsonb)
                )
                ON CONFLICT (
                    sensor_id, endpoint, idempotency_key, transition_type, entity_id
                ) DO NOTHING"""),
        [
            {
                "sensor_id": sensor_id,
                "endpoint": endpoint,
                "idempotency_key": idempotency_key,
                "transition_type": transition_type,
                "entity_id": item[entity_key],
                "payload": json.dumps(item, default=str),
            }
            for item in transitions
        ],
    )


def _is_accepted_timestamp(ts: datetime, now: datetime) -> bool:
    if ts > now + SENSOR_FUTURE_SKEW:
        raise HTTPException(400, "Result timestamp is more than 60 seconds in the future")
    return ts >= now - SENSOR_BACKFILL_WINDOW


async def _insert_clickhouse(table: str, rows: list[list[Any]], column_names: list[str]) -> None:
    """Keep blocking ClickHouse I/O off FastAPI's event loop."""
    await asyncio.to_thread(
        lambda: get_clickhouse_client().insert(table, rows, column_names=column_names)
    )


async def _dispatch_device_transitions(
    transitions: list[dict[str, Any]],
    db: AsyncSession,
    *,
    raise_on_error: bool = False,
) -> None:
    if not transitions:
        return
    try:
        await _insert_clickhouse(
            "device_status_log",
            [
                [
                    item["device_id"], item["timestamp"], item["old_status"],
                    item["new_status"], item["reason"], 0,
                ]
                for item in transitions
            ],
            ["device_id", "timestamp", "old_status", "new_status", "reason", "duration_sec"],
        )
    except Exception:
        logger.exception("failed to write remote-sensor device transition log")
        if raise_on_error:
            raise

    from app.api.v1.alert_engine import StatusChangeEvent, evaluate_status_change
    for item in transitions:
        if item["new_status"] == "maintenance":
            continue
        try:
            await evaluate_status_change(
                StatusChangeEvent(
                    device_id=item["device_id"],
                    hostname=item["hostname"],
                    ip_address=item["ip_address"],
                    old_status=item["old_status"],
                    new_status=item["new_status"],
                    device_type=item.get("device_type"),
                    group_id=item.get("group_id"),
                    location=item.get("location"),
                    rtt_ms=item["rtt_ms"],
                    packet_loss=item["packet_loss"],
                ),
                db,
            )
        except Exception:
            logger.exception(
                "remote-sensor alert evaluation failed for device %s", item["device_id"]
            )
            if raise_on_error:
                raise


@router.post("/results/ping")
async def post_ping_results(
    body: PingResultsBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    await _lock_live_sensor_for_results(sensor, db)
    await _limit_sensor_request(
        request, "ping", sensor_id=x_sensor_id, amount=max(1, len(body.items)), limit=60_000
    )
    if not body.items:
        duplicate = await _reserve_ingest(sensor["id"], "ping", body, db)
        if duplicate:
            return duplicate
        await _complete_ingest(sensor["id"], "ping", body.idempotency_key or "", 0, 0, db)
        await db.commit()
        return {"inserted": 0, "dropped": 0, "duplicate": False}

    rows: list[list] = []
    last_status: dict[str, dict] = {}
    normalized_items: list[tuple[dict, UUID, datetime, bool]] = []
    now = datetime.now(timezone.utc)

    for it in body.items:
        if not isinstance(it, dict):
            it = it.model_dump() if hasattr(it, "model_dump") else dict(it)
        device_uuid = _uuid_from_item(it, "device_id")
        ts = _parse_dt(it.get("timestamp"))
        normalized_items.append((it, device_uuid, ts, _is_accepted_timestamp(ts, now)))

    duplicate = await _reserve_ingest(sensor["id"], "ping", body, db)
    if duplicate:
        return duplicate

    allowed = await _allowed_device_ids(
        sensor["id"], {device_uuid for _, device_uuid, _, _ in normalized_items}, db
    )
    rejected = [
        str(device_uuid)
        for _, device_uuid, _, _ in normalized_items
        if str(device_uuid) not in allowed
    ]
    if rejected:
        raise HTTPException(403, f"Sensor is not assigned to device(s): {', '.join(sorted(set(rejected)))}")

    dropped = 0
    for it, device_uuid, ts, accepted in normalized_items:
        if not accepted:
            dropped += 1
            continue
        device_id = str(device_uuid)
        is_up = 1 if it.get("is_up") else 0
        rtt = float(it.get("rtt_ms") or 0.0)
        packet_loss = float(
            it.get("packet_loss")
            if it.get("packet_loss") is not None
            else (0.0 if is_up else 1.0)
        )
        jitter = float(it.get("jitter_ms") or 0.0)
        min_rtt = float(it.get("min_rtt_ms") if it.get("min_rtt_ms") is not None else rtt)
        max_rtt = float(it.get("max_rtt_ms") if it.get("max_rtt_ms") is not None else rtt)
        packets_sent = int(it.get("packets_sent") if it.get("packets_sent") is not None else 1)
        packets_received = int(
            it.get("packets_received")
            if it.get("packets_received") is not None
            else (1 if is_up else 0)
        )
        raw_ip = it.get("ip_address") or "0.0.0.0"
        parsed_ip = ipaddress.ip_address(raw_ip)
        # The current ClickHouse ping schema is IPv4. Keep IPv6 probes
        # functional without poisoning the durable queue; device identity is
        # authoritative and the live status still records the result.
        ip = str(parsed_ip) if parsed_ip.version == 4 else "0.0.0.0"
        rows.append([
            device_id, ts, is_up, rtt,
            packet_loss, jitter, min_rtt, max_rtt,
            packets_sent, packets_received,
            str(sensor["id"]),
            ip,
        ])
        prior = last_status.get(device_id)
        if prior is None or ts > prior["ts"]:
            last_status[device_id] = {
                "is_up": is_up, "rtt": rtt, "packet_loss": packet_loss,
                "ts": ts, "ip": ip,
            }

    if rows:
        await _insert_clickhouse(
            "ping_metrics",
            rows,
            column_names=[
                "device_id", "timestamp", "is_up", "rtt_ms",
                "packet_loss", "jitter_ms", "min_rtt_ms", "max_rtt_ms",
                "packets_sent", "packets_recv", "poller_id", "ip_address",
            ],
        )

    # Update each device's current state and capture exactly one transition.
    threshold_row = (await db.execute(text("""
        SELECT COALESCE((value->>'degraded_rtt_ms')::float8, 100.0) AS rtt_ms,
               COALESCE((value->>'degraded_loss_pct')::float8, 10.0) AS loss_pct
          FROM system_settings WHERE key = 'monitoring'
    """))).mappings().first()
    degraded_rtt_ms = float(threshold_row["rtt_ms"]) if threshold_row else 100.0
    degraded_loss = (float(threshold_row["loss_pct"]) / 100.0) if threshold_row else 0.10
    transitions: list[dict[str, Any]] = []
    for did, s in sorted(last_status.items()):
        try:
            if not s["is_up"]:
                new_status = "down"
            elif s["rtt"] > degraded_rtt_ms or s["packet_loss"] > degraded_loss:
                new_status = "degraded"
            else:
                new_status = "up"
            changed = (await db.execute(
                text("""WITH current AS (
                            SELECT d.id, d.status AS old_status, d.hostname,
                                   host(d.ip_address)::text AS ip_address,
                                   d.device_type, d.group_id, d.location
                              FROM devices d
                             WHERE d.id = :id
                             FOR UPDATE
                        )
                        , maintenance AS (
                            SELECT EXISTS (
                                SELECT 1
                                  FROM device_maintenance m
                                  JOIN devices md ON md.id = :id AND (
                                         (m.scope_type = 'device' AND m.scope_device_id = md.id)
                                      OR (m.scope_type = 'group' AND m.scope_group_id = md.group_id)
                                      OR (m.scope_type = 'tag' AND jsonb_exists(COALESCE(md.tags, '[]'::jsonb), m.scope_tag))
                                      OR m.scope_type = 'all'
                                  )
                                 WHERE m.starts_at <= NOW() AND m.ends_at >= NOW()
                            ) AS active
                        )
                        UPDATE devices d SET
                          status = CASE WHEN maintenance.active THEN 'maintenance' ELSE :st END,
                          last_seen = :ts,
                          last_rtt_ms = :rtt,
                          updated_at = NOW()
                        FROM current c, maintenance
                        WHERE d.id = c.id
                          AND :ts >= NOW() - make_interval(
                                secs => GREATEST(COALESCE(d.ping_interval, 60) * 2, 60)
                              )
                          AND (d.last_seen IS NULL OR d.last_seen <= :ts)
                          AND EXISTS (
                              SELECT 1 FROM device_polling_owner owner
                               WHERE owner.device_id = d.id
                                 AND owner.owner_kind = 'sensor'
                                 AND owner.sensor_id = :sid
                          )
                        RETURNING c.old_status, d.status AS new_status,
                                  c.hostname, c.ip_address, c.device_type,
                                  c.group_id, c.location"""),
                {
                    "st": new_status,
                    "ts": s["ts"],
                    "rtt": s["rtt"],
                    "id": did,
                    "sid": sensor["id"],
                },
            )).mappings().first()
            if changed and changed["old_status"] != changed["new_status"]:
                transitions.append({
                    **dict(changed),
                    "device_id": did,
                    "group_id": str(changed["group_id"]) if changed["group_id"] else None,
                    "timestamp": s["ts"],
                    "rtt_ms": s["rtt"],
                    "packet_loss": s["packet_loss"],
                    "reason": (
                        f"remote sensor {sensor['name']} reported high latency or packet loss"
                        if new_status == "degraded"
                        else f"remote sensor {sensor['name']}"
                    ),
                })
        except Exception as e:
            logger.warning("device update failed for %s: %s", did, e)
    await _queue_transitions(
        sensor["id"], "ping", body.idempotency_key or "", "device", transitions, db
    )
    await _complete_ingest(
        sensor["id"], "ping", body.idempotency_key or "", len(rows), dropped, db
    )
    await db.commit()

    return {"inserted": len(rows), "dropped": dropped, "duplicate": False}


async def _update_service_vantage_and_consensus(
    service_check_id: str,
    sensor_id: UUID,
    sample: dict[str, Any],
    db: AsyncSession,
) -> Optional[dict[str, Any]]:
    """Apply a fresh sample to its vantage, then recompute aggregate state."""
    before = (await db.execute(
        text("""SELECT sc.status, sc.name, sc.check_type, sc.device_id,
                       sc.group_id, sc.tags, sc.tls_warn_days,
                       sc.tls_critical_days,
                       COALESCE(sc.target_url, sc.target_host, sc.name) AS target,
                       d.hostname AS device_hostname, g.name AS group_name
                  FROM service_checks sc
                  LEFT JOIN devices d ON d.id = sc.device_id
                  LEFT JOIN service_check_groups g ON g.id = sc.group_id
                 WHERE sc.id = :check_id
                 FOR UPDATE OF sc"""),
        {"check_id": service_check_id},
    )).mappings().first()
    if not before:
        return None

    state = "up" if sample["is_up"] else "down"
    if state == "up" and before["check_type"] == "tls":
        if sample.get("tls_valid") is False:
            state = "down"
        elif sample.get("tls_days_remaining") is not None:
            days = int(sample["tls_days_remaining"])
            critical_days = int(before["tls_critical_days"] or 7)
            warn_days = int(before["tls_warn_days"] or 30)
            if days <= critical_days:
                state = "down"
            elif days <= warn_days:
                state = "warning"

    await db.execute(
        text("""INSERT INTO service_check_vantage_status (
                    service_check_id, poller_id, state, last_change_at,
                    last_result_at, last_latency_ms, last_error,
                    last_tls_expiry_date, last_tls_days_remaining,
                    last_tls_valid, last_tls_issuer, last_tls_subject,
                    last_content_matched, updated_at
                )
                SELECT sc.id, :poller_id, :state, :ts, :ts, :latency, :error,
                       :tls_expiry, :tls_days, :tls_valid, :tls_issuer,
                       :tls_subject, :content_matched, NOW()
                  FROM service_checks sc
                 WHERE sc.id = :check_id
                   AND :ts >= NOW() - make_interval(
                         secs => GREATEST(COALESCE(sc.check_interval, 60) * 2, 60)
                       )
                ON CONFLICT (service_check_id, poller_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    last_change_at = CASE
                        WHEN service_check_vantage_status.state <> EXCLUDED.state
                        THEN EXCLUDED.last_result_at
                        ELSE service_check_vantage_status.last_change_at
                    END,
                    last_result_at = EXCLUDED.last_result_at,
                    last_latency_ms = EXCLUDED.last_latency_ms,
                    last_error = EXCLUDED.last_error,
                    last_tls_expiry_date = EXCLUDED.last_tls_expiry_date,
                    last_tls_days_remaining = EXCLUDED.last_tls_days_remaining,
                    last_tls_valid = EXCLUDED.last_tls_valid,
                    last_tls_issuer = EXCLUDED.last_tls_issuer,
                    last_tls_subject = EXCLUDED.last_tls_subject,
                    last_content_matched = EXCLUDED.last_content_matched,
                    updated_at = NOW()
                WHERE service_check_vantage_status.last_result_at <= EXCLUDED.last_result_at"""),
        {
            "check_id": service_check_id,
            "poller_id": str(sensor_id),
            "state": state,
            "ts": sample["ts"],
            "latency": sample["response_ms"],
            "error": sample["error"] or None,
            "tls_expiry": sample.get("tls_expiry_date"),
            "tls_days": sample.get("tls_days_remaining"),
            "tls_valid": sample.get("tls_valid"),
            "tls_issuer": sample.get("tls_issuer") or None,
            "tls_subject": sample.get("tls_subject") or None,
            "content_matched": sample.get("content_matched"),
        },
    )

    updated = (await db.execute(
        text("""WITH policy AS (
                    SELECT id, COALESCE(consensus_mode, 'any') AS mode,
                           COALESCE(consensus_k, 1) AS k,
                           GREATEST(COALESCE(check_interval, 60) * 2, 60) AS freshness_s
                      FROM service_checks WHERE id = :check_id
                ), reporting AS (
                    SELECT v.poller_id, v.state, v.last_result_at,
                           v.last_latency_ms, v.last_error,
                           v.last_tls_expiry_date, v.last_tls_days_remaining,
                           v.last_tls_valid, v.last_tls_issuer,
                           v.last_tls_subject, v.last_content_matched
                      FROM service_check_vantage_status v
                      LEFT JOIN sensors s ON s.id::text = v.poller_id
                      JOIN policy p ON p.id = v.service_check_id
                      JOIN service_monitoring_vantages selected ON selected.service_check_id=v.service_check_id AND selected.poller_id=v.poller_id
                     WHERE (v.poller_id = 'central' OR s.status IN ('online', 'degraded'))
                       AND v.last_result_at >= NOW() - make_interval(secs => p.freshness_s)
                ), counts AS (
                    SELECT COUNT(*)::INTEGER AS total,
                           COUNT(*) FILTER (WHERE state = 'down')::INTEGER AS down_count,
                           COUNT(*) FILTER (WHERE state = 'warning')::INTEGER AS warning_count,
                           MAX(last_result_at) AS newest
                      FROM reporting
                ), latest AS (
                    SELECT * FROM reporting
                     ORDER BY last_result_at DESC, poller_id
                     LIMIT 1
                ), verdict AS (
                    SELECT CASE
                             WHEN c.total = 0 THEN NULL
                             WHEN p.mode = 'majority' AND c.down_count > c.total / 2.0 THEN 'down'
                             WHEN p.mode = 'threshold' AND c.down_count >= p.k THEN 'down'
                             WHEN p.mode = 'any' AND c.down_count >= 1 THEN 'down'
                             WHEN c.warning_count >= 1 THEN 'warning'
                             ELSE 'up'
                           END AS state,
                           c.newest, l.last_latency_ms, l.last_error,
                           l.last_tls_expiry_date, l.last_tls_days_remaining,
                           l.last_tls_valid, l.last_tls_issuer,
                           l.last_tls_subject, l.last_content_matched
                      FROM counts c CROSS JOIN policy p CROSS JOIN latest l
                )
                UPDATE service_checks sc
                   SET status = v.state,
                       last_check_at = v.newest,
                       last_response_ms = v.last_latency_ms,
                       last_error = v.last_error,
                       tls_expiry_date = v.last_tls_expiry_date,
                       tls_days_remaining = v.last_tls_days_remaining,
                       tls_issuer = v.last_tls_issuer,
                       tls_subject = v.last_tls_subject,
                       updated_at = NOW()
                 FROM verdict v
                 WHERE sc.id = :check_id AND v.state IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM service_monitoring_vantages central WHERE central.service_check_id=sc.id AND central.poller_id='central')
                   AND (sc.last_check_at IS NULL OR sc.last_check_at <= v.newest)
                RETURNING sc.status AS new_status, sc.last_check_at,
                          sc.last_response_ms, sc.last_error,
                          sc.tls_expiry_date, sc.tls_days_remaining,
                          sc.tls_issuer, sc.tls_subject"""),
        {"check_id": service_check_id},
    )).mappings().first()
    if not updated or before["status"] == updated["new_status"]:
        return None
    return {
        **dict(before),
        "service_check_id": service_check_id,
        "old_status": before["status"],
        "new_status": updated["new_status"],
        "timestamp": updated["last_check_at"],
        "response_ms": updated["last_response_ms"],
        "error": updated["last_error"],
        "device_id": str(before["device_id"]) if before["device_id"] else None,
        "group_id": str(before["group_id"]) if before["group_id"] else None,
        "tags": list(before["tags"] or []),
    }


async def _dispatch_service_transitions(
    transitions: list[dict[str, Any]],
    db: AsyncSession,
    *,
    raise_on_error: bool = False,
) -> None:
    if not transitions:
        return
    try:
        await _insert_clickhouse(
            "service_status_log",
            [
                [
                    item["service_check_id"],
                    item["device_id"] or "00000000-0000-0000-0000-000000000000",
                    item["timestamp"], item["check_type"], item["old_status"],
                    item["new_status"], "remote sensor consensus", 0,
                ]
                for item in transitions
            ],
            [
                "service_check_id", "device_id", "timestamp", "check_type",
                "old_status", "new_status", "reason", "duration_sec",
            ],
        )
    except Exception:
        logger.exception("failed to write remote-sensor service transition log")
        if raise_on_error:
            raise

    from app.api.v1.alert_engine import (
        ServiceStatusChangeEvent,
        evaluate_service_status_change,
    )
    for item in transitions:
        try:
            await evaluate_service_status_change(
                ServiceStatusChangeEvent(
                    service_check_id=item["service_check_id"],
                    check_name=item["name"],
                    check_type=item["check_type"],
                    old_status=item["old_status"],
                    new_status=item["new_status"],
                    device_id=item["device_id"],
                    device_hostname=item.get("device_hostname"),
                    group_id=item["group_id"],
                    group_name=item.get("group_name"),
                    tags=item["tags"],
                    response_ms=item["response_ms"],
                    error=item["error"],
                    target=item["target"],
                ),
                db,
            )
        except Exception:
            logger.exception(
                "remote-sensor alert evaluation failed for service check %s",
                item["service_check_id"],
            )
            if raise_on_error:
                raise


@router.post("/results/service")
async def post_service_results(
    body: ServiceResultsBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    await _lock_live_sensor_for_results(sensor, db)
    await _limit_sensor_request(
        request, "service", sensor_id=x_sensor_id, amount=max(1, len(body.items)), limit=60_000
    )
    if not body.items:
        duplicate = await _reserve_ingest(sensor["id"], "service", body, db)
        if duplicate:
            return duplicate
        await _complete_ingest(sensor["id"], "service", body.idempotency_key or "", 0, 0, db)
        await db.commit()
        return {"inserted": 0, "dropped": 0, "duplicate": False}

    rows: list[list] = []
    last_status: dict[str, dict] = {}
    normalized_items: list[tuple[dict, UUID, datetime, bool]] = []
    now = datetime.now(timezone.utc)

    for it in body.items:
        if not isinstance(it, dict):
            it = it.model_dump() if hasattr(it, "model_dump") else dict(it)
        check_uuid = _uuid_from_item(it, "service_check_id")
        ts = _parse_dt(it.get("timestamp"))
        normalized_items.append((it, check_uuid, ts, _is_accepted_timestamp(ts, now)))

    duplicate = await _reserve_ingest(sensor["id"], "service", body, db)
    if duplicate:
        return duplicate

    allowed = await _allowed_service_check_ids(
        sensor["id"], {check_uuid for _, check_uuid, _, _ in normalized_items}, db
    )
    rejected = [
        str(check_uuid)
        for _, check_uuid, _, _ in normalized_items
        if str(check_uuid) not in allowed
    ]
    if rejected:
        raise HTTPException(403, f"Sensor is not assigned to service check(s): {', '.join(sorted(set(rejected)))}")

    dropped = 0
    for it, check_uuid, ts, accepted in normalized_items:
        if not accepted:
            dropped += 1
            continue
        sc_id = str(check_uuid)
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
            int(it["tls_days_remaining"]) if it.get("tls_days_remaining") is not None else None,
            bool(it["tls_valid"]) if it.get("tls_valid") is not None else None,
            bool(it["content_matched"]) if it.get("content_matched") is not None else None,
            err if err else None,
            str(sensor["id"]),
        ])
        prior = last_status.get(sc_id)
        if prior is None or ts > prior["ts"]:
            last_status[sc_id] = {
                "is_up": is_up,
                "ts": ts,
                "response_ms": resp_ms,
                "error": str(err)[:2048],
                "tls_expiry_date": it.get("tls_expiry_date"),
                "tls_days_remaining": it.get("tls_days_remaining"),
                "tls_valid": it.get("tls_valid"),
                "tls_issuer": it.get("tls_issuer"),
                "tls_subject": it.get("tls_subject"),
                "content_matched": it.get("content_matched"),
            }

    if rows:
        await _insert_clickhouse(
            "service_metrics",
            rows,
            column_names=[
                "service_check_id", "device_id", "timestamp", "check_type",
                "is_up", "response_ms", "status_code",
                "tls_days_remaining", "tls_valid", "content_matched",
                "error_message", "poller_id",
            ],
        )

    transitions: list[dict[str, Any]] = []
    for sc_id, s in sorted(last_status.items()):
        try:
            transition = await _update_service_vantage_and_consensus(
                sc_id, sensor["id"], s, db
            )
            if transition:
                transitions.append(transition)
        except Exception as e:
            logger.warning("service-check consensus update failed for %s: %s", sc_id, e)
            raise
    await _queue_transitions(
        sensor["id"], "service", body.idempotency_key or "", "service", transitions, db
    )
    await _complete_ingest(
        sensor["id"], "service", body.idempotency_key or "", len(rows), dropped, db
    )
    await db.commit()

    return {"inserted": len(rows), "dropped": dropped, "duplicate": False}


@router.post("/results/snmp")
async def post_snmp_results(
    body: SnmpResultsBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """Store scalar SNMP samples for devices owned by this sensor."""
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    await _lock_live_sensor_for_results(sensor, db)
    await _limit_sensor_request(
        request, "snmp", sensor_id=x_sensor_id, amount=max(1, len(body.items)), limit=60_000
    )
    now = datetime.now(timezone.utc)
    normalized: list[tuple[dict[str, Any], UUID, datetime, bool]] = []
    for raw in body.items or []:
        item = raw if isinstance(raw, dict) else raw.model_dump()
        device_id = _uuid_from_item(item, "device_id")
        ts = _parse_dt(item.get("timestamp"))
        normalized.append((item, device_id, ts, _is_accepted_timestamp(ts, now)))

    duplicate = await _reserve_ingest(sensor["id"], "snmp", body, db)
    if duplicate:
        return duplicate
    allowed = await _allowed_device_ids(
        sensor["id"], {device_id for _, device_id, _, _ in normalized}, db
    )
    rejected_ids = [
        str(device_id)
        for _, device_id, _, _ in normalized
        if str(device_id) not in allowed
    ]
    if rejected_ids:
        raise HTTPException(
            403,
            f"Sensor is not assigned to device(s): {', '.join(sorted(set(rejected_ids)))}",
        )

    rows: list[list[Any]] = []
    dropped = 0
    for item, device_id, ts, accepted in normalized:
        if not accepted:
            dropped += 1
            continue
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        oid = str(item.get("oid") or "").strip()
        if not oid:
            dropped += 1
            continue
        rows.append([
            str(device_id), oid[:255], value, str(item.get("unit") or "")[:32],
            ts, str(sensor["id"]),
        ])

    if rows:
        await _insert_clickhouse(
            "snmp_metrics",
            rows,
            column_names=[
                "device_id", "metric_key", "value", "unit", "timestamp", "poller_id",
            ],
        )
    await _complete_ingest(
        sensor["id"], "snmp", body.idempotency_key or "", len(rows), dropped, db
    )
    await db.commit()
    return {"inserted": len(rows), "dropped": dropped, "duplicate": False}


# ── Events ───────────────────────────────────────────────────────────

@router.post("/events")
async def post_events(
    body: EventsBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_sensor_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    sensor = await _authenticate(x_sensor_id, _strip_bearer(authorization), db)
    await _limit_sensor_request(
        request, "events", sensor_id=x_sensor_id, amount=max(1, len(body.items)), limit=10_000
    )
    items = body.items or []
    if items:
        params = []
        for event in items:
            event_data = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
            event_kind = str(event_data.get("type") or "sensor_event")[:64]
            detail_data = event_data.get("data") or {}
            if event_kind in {"command_completed", "command_failed"}:
                command_id = detail_data.get("command_id")
                try:
                    command_uuid = UUID(str(command_id))
                except (TypeError, ValueError):
                    command_uuid = None
                if command_uuid:
                    message = str(detail_data.get("message") or "")[:2048]
                    version_value = str(detail_data.get("version") or "").strip()
                    if version_value:
                        message = f"{message} (version {version_value})".strip()
                    await db.execute(
                        text("""UPDATE sensor_commands
                                   SET status = :status,
                                       completed_at = :completed_at,
                                       result = :result
                                 WHERE id = :command_id
                                   AND sensor_id = :sensor_id
                                   AND status IN ('pending', 'delivered')"""),
                        {
                            "status": "succeeded" if event_kind == "command_completed" else "failed",
                            "completed_at": _parse_dt(event_data.get("timestamp")),
                            "result": message or event_kind,
                            "command_id": command_uuid,
                            "sensor_id": sensor["id"],
                        },
                    )
            params.append({
                "sensor_id": sensor["id"],
                "ts": _parse_dt(event_data.get("timestamp")),
                "kind": event_kind,
                "detail": json.dumps({
                    "target_type": event_data.get("target_type"),
                    "target_id": event_data.get("target_id"),
                    "data": detail_data,
                }),
            })
        await db.execute(
            text("""INSERT INTO sensor_events (sensor_id, ts, kind, detail)
                    VALUES (:sensor_id, :ts, :kind, CAST(:detail AS jsonb))"""),
            params,
        )
        await db.commit()
    logger.info("sensor %s events: %d", sensor["id"], len(items))
    return {"accepted": len(items)}


# ── Sensor appliance downloads ───────────────────────────────────────

def _artifact_path(kind: str) -> Path:
    filename = SENSOR_ARTIFACT_BASENAMES.get(kind)
    if not filename:
        raise HTTPException(404, "Unknown sensor appliance artifact")
    return SENSOR_ARTIFACT_DIR / filename


def _artifact_info(kind: str, request: Request) -> dict:
    path = _artifact_path(kind)
    url = _public_route_url(
        request,
        "download_sensor_appliance_artifact",
        kind=kind,
    )
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
            *[_artifact_info(kind, request) for kind in ("qcow2", "vhdx") if _artifact_path(kind).is_file()],
        ],
        "bootstrap": {
            "method": "cloud-init NoCloud seed ISO",
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


# ── Ubuntu sensor binary and one-line installer ─────────────────────

@router.get("/bin/{platform}/manifest.json", name="sensor_binary_manifest")
async def sensor_binary_manifest(platform: str, request: Request):
    binary = _binary_path(platform)
    available = binary.exists() and binary.is_file()
    sha256 = None
    if available:
        sha256 = _binary_sha_line(platform).split()[0]
    os_name, arch = SUPPORTED_SENSOR_BINARY_PLATFORMS[platform]
    metadata = _binary_metadata(platform)
    signed_metadata, signed_manifest, signature = _signed_binary_metadata(platform)
    if signed_metadata:
        # Keep the signed update endpoint deliberately tiny. The runtime first
        # verifies these exact bytes with its embedded release key and only then
        # trusts the inner version/platform/URL/digest fields.
        return {
            "signed_manifest": base64.b64encode(signed_manifest).decode("ascii"),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    binary_url = _public_route_url(
        request, "download_sensor_binary", platform=platform
    )
    sha256_url = _public_route_url(
        request, "download_sensor_binary_checksum", platform=platform
    )
    return {
        "product": "ZenPlus Remote Sensor",
        "platform": platform,
        "os": os_name,
        "arch": arch,
        "version": metadata.get("version"),
        "available": available,
        "self_update_available": False,
        "filename": SENSOR_BINARY_NAME,
        "url": binary_url,
        "binary_url": binary_url,
        "sha256_url": sha256_url,
        "size_bytes": binary.stat().st_size if available else None,
        "updated_at": datetime.fromtimestamp(binary.stat().st_mtime, tz=timezone.utc) if available else None,
        "sha256": sha256,
    }


@router.get("/bin/{platform}/zenplus-sensor", name="download_sensor_binary")
async def download_sensor_binary(platform: str):
    binary = _binary_path(platform)
    if not binary.exists() or not binary.is_file():
        raise HTTPException(
            404,
            f"Sensor binary for {platform} is not published yet. "
            "Build the appliance release or run the installer build step first.",
        )
    return FileResponse(binary, media_type="application/octet-stream", filename=SENSOR_BINARY_NAME)


@router.get(
    "/bin/{platform}/zenplus-sensor.sha256",
    response_class=PlainTextResponse,
    name="download_sensor_binary_checksum",
)
async def download_sensor_binary_checksum(platform: str):
    return _binary_sha_line(platform)

INSTALL_SH = """\
#!/usr/bin/env bash
# ZenPlus remote sensor installer for Ubuntu.
#
# Required env:
#   ZENPLUS_SERVER_URL=https://central
#   ZENPLUS_ENROLLMENT_TOKEN=zps_enr_...
#   ZENPLUS_SENSOR_NAME=branch-name
# Optional: ZENPLUS_PROXY_URL=http://proxy.example:3128
set -euo pipefail
: "${ZENPLUS_SERVER_URL:?set ZENPLUS_SERVER_URL}"
: "${ZENPLUS_ENROLLMENT_TOKEN:?set ZENPLUS_ENROLLMENT_TOKEN}"
: "${ZENPLUS_SENSOR_NAME:?set ZENPLUS_SENSOR_NAME}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This installer must run as root. Use: curl ... | sudo env ... bash" >&2
  exit 1
fi

for value in "$ZENPLUS_SERVER_URL" "$ZENPLUS_ENROLLMENT_TOKEN" "$ZENPLUS_SENSOR_NAME" "${ZENPLUS_PROXY_URL:-}"; do
  if [[ "$value" == *$'\\n'* || "$value" == *$'\\r'* ]]; then
    echo "Installer environment values must not contain newlines." >&2
    exit 1
  fi
done

case "$(uname -m)" in
  x86_64|amd64) PLATFORM="linux-amd64" ;;
  *) echo "Unsupported CPU architecture: $(uname -m); this release publishes linux-amd64 only." >&2; exit 1 ;;
esac

if [[ "${ZENPLUS_VERIFY_TLS:-1}" != "1" ]]; then
  echo "ZENPLUS_VERIFY_TLS cannot disable controller certificate verification." >&2
  exit 1
fi
VERIFY_TLS="1"
ENV_DIR="/etc/zenplus-sensor"
ENV_FILE="$ENV_DIR/sensor.env"
if [[ -n "${ZENPLUS_SENSOR_STATE_DIR:-}" && "${ZENPLUS_SENSOR_STATE_DIR}" != "/var/lib/zenplus-sensor" ]]; then
  echo "ZENPLUS_SENSOR_STATE_DIR overrides are not supported by the hardened service unit." >&2
  exit 1
fi
STATE_DIR="/var/lib/zenplus-sensor"
INSTALL_BIN="$STATE_DIR/bin/zenplus-sensor"
LOG_DIR="/var/log/zenplus-sensor"
SERVICE_FILE="/etc/systemd/system/zenplus-sensor.service"
if [[ -s "$STATE_DIR/state.json" ]]; then
  echo "An enrolled ZenPlus sensor identity already exists at $STATE_DIR/state.json." >&2
  echo "Refusing to ignore the new enrollment token. Decommission or explicitly archive the old state before reprovisioning this VM." >&2
  exit 1
fi
TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

curl_args=(-fsSL)
if [[ -n "${ZENPLUS_PROXY_URL:-}" ]]; then
  export HTTP_PROXY="$ZENPLUS_PROXY_URL" HTTPS_PROXY="$ZENPLUS_PROXY_URL"
  export http_proxy="$ZENPLUS_PROXY_URL" https_proxy="$ZENPLUS_PROXY_URL"
  curl_args+=(--proxy "$ZENPLUS_PROXY_URL")
fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates iputils-ping libcap2-bin >/dev/null
fi

if [[ -n "${ZENPLUS_CA_CERT_B64:-}" ]]; then
  printf '%s' "$ZENPLUS_CA_CERT_B64" | base64 -d > "$TMP_DIR/zenplus-controller.crt"
  grep -q '^-----BEGIN CERTIFICATE-----' "$TMP_DIR/zenplus-controller.crt" || {
    echo "Invalid ZenPlus controller CA certificate." >&2
    exit 1
  }
  install -m 0644 -o root -g root "$TMP_DIR/zenplus-controller.crt" \
    /usr/local/share/ca-certificates/zenplus-controller.crt
  update-ca-certificates >/dev/null
fi

if ! getent group zenplus-sensor >/dev/null; then
  groupadd --system zenplus-sensor
fi
if ! id zenplus-sensor >/dev/null 2>&1; then
  useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin --gid zenplus-sensor zenplus-sensor
fi

install -d -m 0770 -o root -g zenplus-sensor "$ENV_DIR"
install -d -m 0700 -o zenplus-sensor -g zenplus-sensor "$STATE_DIR" "$STATE_DIR/bin"
install -d -m 0750 -o zenplus-sensor -g zenplus-sensor "$LOG_DIR"

BIN_URL="$ZENPLUS_SERVER_URL/api/v1/sensor/bin/$PLATFORM/zenplus-sensor"
SHA_URL="$ZENPLUS_SERVER_URL/api/v1/sensor/bin/$PLATFORM/zenplus-sensor.sha256"
echo "Downloading ZenPlus sensor binary for $PLATFORM..."
curl "${curl_args[@]}" "$BIN_URL" -o "$TMP_DIR/zenplus-sensor"
curl "${curl_args[@]}" "$SHA_URL" -o "$TMP_DIR/zenplus-sensor.sha256"
( cd "$TMP_DIR" && sha256sum -c zenplus-sensor.sha256 )

install -m 0700 -o zenplus-sensor -g zenplus-sensor "$TMP_DIR/zenplus-sensor" "$INSTALL_BIN"

: > "$ENV_FILE"
write_env_line() {
  local key="$1" value="$2"
  value="${value//\\\\/\\\\\\\\}"
  value="${value//\\\"/\\\\\\\"}"
  printf '%s="%s"\\n' "$key" "$value" >> "$ENV_FILE"
}
write_env_line ZENPLUS_SERVER_URL "$ZENPLUS_SERVER_URL"
write_env_line ZENPLUS_ENROLLMENT_TOKEN "$ZENPLUS_ENROLLMENT_TOKEN"
write_env_line ZENPLUS_SENSOR_NAME "$ZENPLUS_SENSOR_NAME"
write_env_line ZENPLUS_VERIFY_TLS "$VERIFY_TLS"
write_env_line ZENPLUS_SENSOR_STATE_DIR "$STATE_DIR"
write_env_line ZENPLUS_SENSOR_ENV_FILE "$ENV_FILE"
write_env_line ZENPLUS_HEARTBEAT_INTERVAL_SECONDS 30
write_env_line ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS 60
write_env_line ZENPLUS_UPLOAD_INTERVAL_SECONDS 10
write_env_line ZENPLUS_MAX_WORKERS 100
write_env_line ZENPLUS_SPOOL_MAX_MB 512
write_env_line ZENPLUS_SPOOL_RETENTION_HOURS 72
if [[ -n "${ZENPLUS_PROXY_URL:-}" ]]; then
  write_env_line HTTP_PROXY "$ZENPLUS_PROXY_URL"
  write_env_line HTTPS_PROXY "$ZENPLUS_PROXY_URL"
  write_env_line NO_PROXY "localhost,127.0.0.1,::1"
fi
chown zenplus-sensor:zenplus-sensor "$ENV_FILE"
chmod 0600 "$ENV_FILE"

cat > "$SERVICE_FILE" <<SVCEOF
[Unit]
Description=ZenPlus Remote Sensor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zenplus-sensor
Group=zenplus-sensor
EnvironmentFile=/etc/zenplus-sensor/sensor.env
ExecStart=$INSTALL_BIN
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/zenplus-sensor /var/log/zenplus-sensor /etc/zenplus-sensor
CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable --now zenplus-sensor.service
sleep 2

echo
echo "ZenPlus sensor installed."
systemctl --no-pager --full status zenplus-sensor.service || true
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
