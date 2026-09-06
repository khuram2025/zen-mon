"""Admin Sensors API — dashboard-facing CRUD for remote sensor management.

All routes require an authenticated dashboard user. The companion module
``sensor_api.py`` exposes the runtime endpoints (``/api/v1/sensor/*``) that
remote sensor binaries call with their own bearer tokens.

Routes
------
    GET    /api/v1/sensors                  list sensors (with site name + assignment count)
    POST   /api/v1/sensors                  create sensor + issue enrollment token
    GET    /api/v1/sensors/{id}             one sensor with full detail
    PUT    /api/v1/sensors/{id}             update name/description/site/location/tags/status
    DELETE /api/v1/sensors/{id}             delete (also cascades assignments)
    POST   /api/v1/sensors/{id}/regenerate-token   issue a fresh enrollment token
    POST   /api/v1/sensors/{id}/rotate-key  guarded; unsafe in-place rotation is rejected
    POST   /api/v1/sensors/{id}/disable     mark status='disabled'
    POST   /api/v1/sensors/{id}/enable      flip back to 'pending'/'online'

    GET    /api/v1/sensors/{id}/assignments
    PUT    /api/v1/sensors/{id}/assignments  bulk replace

    GET    /api/v1/sites                    list sites
    POST   /api/v1/sites                    create site
    PUT    /api/v1/sites/{id}               update
    DELETE /api/v1/sites/{id}               delete (sensors keep null site_id)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import require_permission
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.sensor_health_service import resolve_sensor_alert
from app.schemas.sensor import (
    SensorCreate,
    SensorUpdate,
    SensorResponse,
    SensorTokenResponse,
    SensorDownloadsResponse,
    SensorEventResponse,
    SensorCommandCreate,
    SensorCommandResponse,
    SensorVantageResponse,
    SensorRotateKeyResponse,
    SiteCreate,
    SiteUpdate,
    SiteResponse,
    AssignmentBulk,
    AssignmentResponse,
)

router = APIRouter(prefix="/sensors", tags=["Sensors"])
sites_router = APIRouter(prefix="/sites", tags=["Sites"])

SENSOR_VIEW = require_permission("settings.view", "settings.manage")
SENSOR_MANAGE = require_permission("settings.manage")


def purge_sensor_bootstrap_artifacts(sensor_id: UUID) -> None:
    """Remove secret-bearing bootstrap media for exactly one sensor."""
    target = BOOTSTRAP_DIR / str(sensor_id)
    if target.parent.resolve() != BOOTSTRAP_DIR.resolve():
        raise RuntimeError("refusing to purge outside sensor bootstrap directory")
    if target.exists():
        shutil.rmtree(target)


async def _require_bootstrap_access(sensor_id: UUID, db: AsyncSession) -> None:
    row = (await db.execute(
        text("""SELECT enrollment_consumed_at, enrollment_expires_at
                  FROM sensors WHERE id = :id"""),
        {"id": sensor_id},
    )).mappings().first()
    now = datetime.now(timezone.utc)
    expires = row.get("enrollment_expires_at") if row else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not row or row.get("enrollment_consumed_at") or not expires or expires <= now:
        await asyncio.to_thread(purge_sensor_bootstrap_artifacts, sensor_id)
        raise HTTPException(410, "Bootstrap artifact expired or enrollment token was consumed")


# ── Helpers ──────────────────────────────────────────────────────────

ENROLLMENT_TTL_HOURS = 24
TOKEN_PREFIX = "zps_"      # zenplus sensor
BOOTSTRAP_DIR = Path(os.getenv("ZENPLUS_SENSOR_BOOTSTRAP_DIR", "/opt/zenplus/artifacts/sensors/bootstrap"))
MIN_SUPPORTED_SENSOR_VERSION = os.getenv("ZENPLUS_MIN_SENSOR_VERSION", "").strip() or None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_enrollment_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hex). Plaintext shown once, hash stored."""
    raw = TOKEN_PREFIX + "enr_" + secrets.token_urlsafe(24)
    return raw, _hash_token(raw)


def _new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext, sha256_hex, prefix-for-display)."""
    raw = TOKEN_PREFIX + "key_" + secrets.token_urlsafe(32)
    return raw, _hash_token(raw), raw[:12]


def _server_url(request: Request) -> str:
    """Build the URL that sensors should call back to.

    Honors X-Forwarded-* headers from nginx, otherwise falls back to
    request.base_url (host only, no path).
    """
    configured = (get_settings().APP_BASE_URL or "").strip().rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _controller_ca_pem(server_url: str) -> Optional[str]:
    if not server_url.lower().startswith("https://"):
        return None
    path = Path(os.getenv("ZENPLUS_CONTROLLER_CA_CERT", "/etc/zenplus/tls/fullchain.pem"))
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if "-----BEGIN CERTIFICATE-----" not in value or len(value) > 128_000:
        return None
    return value + "\n"


def _install_command(
    server_url: str,
    token: str,
    name: str,
    proxy_url: Optional[str] = None,
    ca_pem: Optional[str] = None,
) -> str:
    """A copy-pasteable one-liner the operator runs on the sensor VM.

    The installer runs as root because it creates a service user, installs the
    binary, writes systemd config, and starts the service.
    """
    install_url = shlex.quote(f"{server_url}/api/v1/sensor/install.sh")
    curl_proxy = f"--proxy {shlex.quote(proxy_url)} " if proxy_url else ""
    env_args = (
        f"ZENPLUS_SERVER_URL={shlex.quote(server_url)} "
        f"ZENPLUS_ENROLLMENT_TOKEN={shlex.quote(token)} "
        f"ZENPLUS_SENSOR_NAME={shlex.quote(name)} "
    )
    if proxy_url:
        env_args += f"ZENPLUS_PROXY_URL={shlex.quote(proxy_url)} "
    if not ca_pem:
        return (
            "ZP_INSTALL=$(mktemp) || exit $?; "
            f"curl {curl_proxy}-fsSL {install_url} -o \"$ZP_INSTALL\" && "
            f"sudo env {env_args}bash \"$ZP_INSTALL\"; "
            "ZP_RC=$?; rm -f \"$ZP_INSTALL\"; exit $ZP_RC"
        )
    ca_b64 = base64.b64encode(ca_pem.encode("utf-8")).decode("ascii")
    quoted_ca = shlex.quote(ca_b64)
    return (
        "ZP_CA=$(mktemp) && ZP_INSTALL=$(mktemp) && "
        f"printf %s {quoted_ca} | base64 -d > \"$ZP_CA\" && "
        f"curl {curl_proxy}--cacert \"$ZP_CA\" -fsSL {install_url} -o \"$ZP_INSTALL\" && "
        f"sudo env {env_args}ZENPLUS_CA_CERT_B64={quoted_ca} bash \"$ZP_INSTALL\"; "
        "ZP_RC=$?; rm -f \"$ZP_CA\" \"$ZP_INSTALL\"; exit $ZP_RC"
    )


def _appliance_urls(server_url: str) -> dict[str, str]:
    base = f"{server_url}/api/v1/sensor/appliance"
    return {
        "manifest_url": f"{base}/manifest",
        "ova_url": f"{base}/ova",
        "ovf_url": f"{base}/ovf",
    }


def _artifact_mtime(paths: list[Path]) -> Optional[datetime]:
    existing = [p for p in paths if p.exists() and p.is_file()]
    if not existing:
        return None
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in existing), tz=timezone.utc)


def _latest_sensor_artifact_dir(sensor_id: UUID) -> Optional[Path]:
    root = BOOTSTRAP_DIR / str(sensor_id)
    if not root.exists() or not root.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        artifacts = [
            child / "zenplus-sensor-configured.ova",
            child / "zenplus-sensor-seed.iso",
        ]
        existing = [p for p in artifacts if p.exists() and p.is_file()]
        if existing:
            candidates.append((max(p.stat().st_mtime for p in existing), child))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _systemd_env_line(key: str, value: Any) -> str:
    value = str(value)
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise HTTPException(400, f"{key} must be a single-line environment value")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _sensor_env(server_url: str, token: str, name: str, proxy_url: Optional[str] = None) -> str:
    values: list[tuple[str, Any]] = [
        ("ZENPLUS_SERVER_URL", server_url),
        ("ZENPLUS_ENROLLMENT_TOKEN", token),
        ("ZENPLUS_SENSOR_NAME", name),
        ("ZENPLUS_VERIFY_TLS", 1),
        ("ZENPLUS_SENSOR_STATE_DIR", "/var/lib/zenplus-sensor"),
        ("ZENPLUS_SENSOR_ENV_FILE", "/etc/zenplus-sensor/sensor.env"),
        ("ZENPLUS_HEARTBEAT_INTERVAL_SECONDS", 30),
        ("ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS", 60),
        ("ZENPLUS_UPLOAD_INTERVAL_SECONDS", 10),
        ("ZENPLUS_MAX_WORKERS", 100),
        ("ZENPLUS_SPOOL_MAX_MB", 512),
        ("ZENPLUS_SPOOL_RETENTION_HOURS", 72),
    ]
    if proxy_url:
        values.extend([
            ("HTTP_PROXY", proxy_url),
            ("HTTPS_PROXY", proxy_url),
            ("NO_PROXY", "localhost,127.0.0.1,::1"),
        ])
    return "\n".join(_systemd_env_line(key, value) for key, value in values)


def _console_user_cloud_init(data: Optional[SensorCreate]) -> str:
    if not data or not data.enable_console_user:
        return ""
    if not data.console_username or not data.console_password:
        raise HTTPException(400, "Console user requires username and password")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", data.console_username):
        raise HTTPException(
            400,
            "Console username must start with a lowercase letter or underscore and contain only lowercase letters, numbers, underscore, or dash",
        )
    username = json.dumps(data.console_username)
    from passlib.hash import sha512_crypt
    password = json.dumps(sha512_crypt.using(rounds=200000).hash(data.console_password))
    return f"""
users:
  - default
  - name: {username}
    gecos: ZenPlus Sensor Administrator
    groups: [adm, sudo]
    shell: /bin/bash
    lock_passwd: false
    passwd: {password}
    sudo: ["ALL=(ALL) ALL"]
chpasswd:
  expire: false
"""


def _bootstrap_cloud_init(
    server_url: str,
    token: str,
    name: str,
    proxy_url: Optional[str] = None,
    create_data: Optional[SensorCreate] = None,
    ca_pem: Optional[str] = None,
) -> str:
    """Cloud-init user-data for the production sensor appliance.

    The enrollment token is plaintext and shown only once, so this payload is
    returned only in create/regenerate responses. Operators attach it as a
    NoCloud seed ISO to the immutable base appliance.
    """
    env = "\n".join(f"      {line}" for line in _sensor_env(server_url, token, name, proxy_url).splitlines())
    console_user = _console_user_cloud_init(create_data)
    ssh_password_auth = "true" if create_data and create_data.enable_console_user else "false"
    ca_file = ""
    ca_command = ""
    if ca_pem:
        ca_content = "\n".join(f"      {line}" for line in ca_pem.rstrip().splitlines())
        ca_file = f"""  - path: /usr/local/share/ca-certificates/zenplus-controller.crt
    owner: root:root
    permissions: '0644'
    content: |
{ca_content}
"""
        ca_command = "  - [ update-ca-certificates ]\n"
    return f"""#cloud-config
{console_user}ssh_pwauth: {ssh_password_auth}
disable_root: true
write_files:
  - path: /etc/ssh/sshd_config.d/00-zenplus-security.conf
    owner: root:root
    permissions: '0644'
    content: |
      PermitRootLogin no
      X11Forwarding no
      AllowTcpForwarding no
      AllowAgentForwarding no
      PermitTunnel no
      MaxAuthTries 3
      LoginGraceTime 30
{ca_file}  - path: /etc/zenplus-sensor/sensor.env
    owner: zenplus-sensor:zenplus-sensor
    permissions: '0600'
    content: |
{env}
runcmd:
{ca_command}  - [ /usr/sbin/sshd, -t ]
  - [ systemctl, reload, ssh ]
  - [ systemctl, enable, --now, zenplus-sensor.service ]
"""


def _bootstrap_meta_data(sensor_id: UUID, name: str) -> str:
    hostname = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:63]
    hostname = hostname or f"sensor-{str(sensor_id)[:8]}"
    return f"""instance-id: zenplus-sensor-{sensor_id}
local-hostname: {hostname}
"""


def _bootstrap_network_config(data: SensorCreate) -> Optional[str]:
    if data.network_mode == "dhcp":
        return None
    if not data.sensor_ip or not data.sensor_cidr or not data.gateway:
        raise HTTPException(400, "Static network mode requires sensor_ip, sensor_cidr, and gateway")
    dns = data.dns_servers or ["1.1.1.1", "8.8.8.8"]
    dns_yaml = ", ".join(dns)
    return f"""version: 2
ethernets:
  default:
    match:
      name: "e*"
    dhcp4: false
    dhcp6: false
    addresses: [{data.sensor_ip}/{data.sensor_cidr}]
    routes:
      - to: default
        via: {data.gateway}
    nameservers:
      addresses: [{dns_yaml}]
"""


def _nonsecret_bootstrap_config(data: SensorCreate) -> dict[str, object]:
    """Persist only values required to reproduce deployment media."""
    return {
        "controller_url": data.controller_url,
        "authorization_pending": True,
        "proxy_url": data.proxy_url,
        "network_mode": data.network_mode,
        "sensor_ip": data.sensor_ip,
        "sensor_cidr": data.sensor_cidr,
        "gateway": data.gateway,
        "dns_servers": list(data.dns_servers),
    }


def _write_bootstrap_iso(
    sensor_id: UUID,
    download_token: str,
    user_data: str,
    meta_data: str,
    network_config: Optional[str],
) -> Optional[str]:
    cloud_localds = shutil.which("cloud-localds")
    if not cloud_localds:
        raise RuntimeError(
            "cloud-localds is unavailable; install the cloud-image-utils package"
        )

    sensor_dir = BOOTSTRAP_DIR / str(sensor_id)
    sensor_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sensor_dir.chmod(0o700)
    dest_dir = sensor_dir / download_token
    dest_dir.mkdir(exist_ok=True, mode=0o700)
    dest_dir.chmod(0o700)
    iso_path = dest_dir / "zenplus-sensor-seed.iso"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_data_path = tmp_path / "user-data"
        meta_data_path = tmp_path / "meta-data"
        network_path = tmp_path / "network-config"
        user_data_path.write_text(user_data)
        meta_data_path.write_text(meta_data)
        cmd = [cloud_localds]
        if network_config:
            network_path.write_text(network_config)
            cmd.extend(["-N", str(network_path)])
        cmd.extend([str(iso_path), str(user_data_path), str(meta_data_path)])
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            raise HTTPException(500, f"Failed to generate sensor bootstrap ISO: {exc.stderr.strip()}")
    iso_path.chmod(0o600)
    return download_token


def _write_configured_ova(
    sensor_id: UUID,
    download_token: str,
    server_url: str,
    enrollment_token: str,
    sensor_name: str,
    create_data: Optional[SensorCreate],
) -> Optional[str]:
    builder = "/usr/local/sbin/zenplus-build-configured-sensor-ova"
    if not Path(builder).exists():
        return None

    sensor_dir = BOOTSTRAP_DIR / str(sensor_id)
    sensor_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sensor_dir.chmod(0o700)
    dest_dir = sensor_dir / download_token
    dest_dir.mkdir(exist_ok=True, mode=0o700)
    dest_dir.chmod(0o700)
    config_path = dest_dir / "configured-ova.json"
    payload = {
        "sensor_id": str(sensor_id),
        "sensor_name": sensor_name,
        "server_url": server_url,
        "enrollment_token": enrollment_token,
        "network_mode": create_data.network_mode if create_data else "dhcp",
        "sensor_ip": create_data.sensor_ip if create_data else None,
        "sensor_cidr": create_data.sensor_cidr if create_data else None,
        "gateway": create_data.gateway if create_data else None,
        "dns_servers": create_data.dns_servers if create_data else [],
        "proxy_url": create_data.proxy_url if create_data else None,
        "controller_ca_pem": _controller_ca_pem(server_url),
        "enable_console_user": bool(create_data and create_data.enable_console_user),
        "console_username": (
            create_data.console_username
            if create_data and create_data.enable_console_user
            else None
        ),
        "console_password": (
            create_data.console_password
            if create_data and create_data.enable_console_user
            else None
        ),
    }
    config_path.write_text(json.dumps(payload))
    config_path.chmod(0o600)
    try:
        subprocess.run(
            ["sudo", "-n", builder, "--config-json", str(config_path), "--out-dir", str(dest_dir)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "Timed out generating configured sensor OVA")
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"Failed to generate configured sensor OVA: {exc.stderr.strip()}")
    finally:
        config_path.unlink(missing_ok=True)
    ova_path = dest_dir / "zenplus-sensor-configured.ova"
    if ova_path.exists():
        ova_path.chmod(0o600)
    return download_token if ova_path.exists() else None


async def _token_response(
    sensor_id: UUID,
    token: str,
    expires: datetime,
    server: str,
    name: str,
    create_data: Optional[SensorCreate] = None,
) -> SensorTokenResponse:
    urls = _appliance_urls(server)
    controller_url = (create_data.controller_url if create_data and create_data.controller_url else server)
    proxy_url = create_data.proxy_url if create_data else None
    ca_pem = _controller_ca_pem(controller_url)
    user_data = _bootstrap_cloud_init(
        controller_url, token, name, proxy_url, create_data, ca_pem
    )
    meta_data = _bootstrap_meta_data(sensor_id, name)
    network_config = _bootstrap_network_config(create_data) if create_data else None
    download_token = secrets.token_urlsafe(24)
    bootstrap_warning = None
    try:
        bootstrap_token = await asyncio.to_thread(
            _write_bootstrap_iso,
            sensor_id,
            download_token,
            user_data,
            meta_data,
            network_config,
        )
        # Building a configured OVA can exceed the dashboard request timeout
        # and embeds this one-time token. Registration therefore uses the
        # immutable base OVA plus the short-lived NoCloud seed generated above.
        configured_ova_token = None
    except Exception as exc:
        # Artifact generation is optional. Never make an already-committed
        # enrollment token impossible to deliver to the administrator.
        logging.getLogger("zenplus.sensors").warning(
            "sensor bootstrap artifact generation failed for %s: %s",
            sensor_id,
            exc,
        )
        bootstrap_token = None
        configured_ova_token = None
        bootstrap_warning = f"Seed ISO generation failed: {exc}"
    bootstrap_iso_url = (
        f"{server}/api/v1/sensors/bootstrap/{sensor_id}/{bootstrap_token}/seed.iso"
        if bootstrap_token else None
    )
    configured_ova_url = (
        f"{server}/api/v1/sensors/bootstrap/{sensor_id}/{configured_ova_token}/configured.ova"
        if configured_ova_token else None
    )
    return SensorTokenResponse(
        sensor_id=str(sensor_id),
        enrollment_token=token,
        expires_at=expires,
        server_url=controller_url,
        install_command=_install_command(
            controller_url, token, name, proxy_url, ca_pem
        ),
        bootstrap_cloud_init=user_data,
        bootstrap_meta_data=meta_data,
        bootstrap_network_config=network_config,
        bootstrap_iso_url=bootstrap_iso_url,
        configured_ova_url=configured_ova_url,
        bootstrap_warning=bootstrap_warning,
        **urls,
    )


_SENSOR_LIST_SQL = """
    SELECT s.*,
           si.name AS site_name,
           COALESCE(a.cnt, 0) AS assignment_count
    FROM sensors s
    LEFT JOIN sites si ON si.id = s.site_id
    LEFT JOIN (
        SELECT sensor_id, COUNT(*) AS cnt FROM sensor_assignments GROUP BY sensor_id
    ) a ON a.sensor_id = s.id
"""


def _row_to_sensor(r: dict) -> SensorResponse:
    return SensorResponse(
        authorization_pending=bool((r.get("bootstrap_config") or {}).get("authorization_pending")),
        id=str(r["id"]),
        name=r["name"],
        description=r.get("description"),
        site_id=str(r["site_id"]) if r.get("site_id") else None,
        site_name=r.get("site_name"),
        location=r.get("location"),
        status=r["status"],
        status_reason=r.get("status_reason"),
        version=r.get("version"),
        last_seen_at=r.get("last_seen_at"),
        last_heartbeat_at=r.get("last_heartbeat_at"),
        last_ip=str(r["last_ip"]) if r.get("last_ip") else None,
        queue_depth=r.get("queue_depth", 0) or 0,
        queue_dropped_count=r.get("queue_dropped_count", 0) or 0,
        heartbeat_interval_s=r.get("heartbeat_interval_s", 30) or 30,
        degraded_after_s=r.get("degraded_after_s", 90) or 90,
        offline_after_s=r.get("offline_after_s", 180) or 180,
        min_supported_version=r.get("min_supported_version"),
        hostname=r.get("hostname"),
        os_info=r.get("os_info"),
        uptime_seconds=r.get("uptime_seconds"),
        api_key_prefix=r.get("api_key_prefix"),
        enrollment_pending=bool(r.get("enrollment_token_hash")) and not r.get("enrollment_consumed_at"),
        enrollment_expires_at=r.get("enrollment_expires_at"),
        assignment_count=r.get("assignment_count", 0) or 0,
        tags=list(r.get("tags") or []),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _row_to_command(row: dict) -> SensorCommandResponse:
    return SensorCommandResponse(
        id=str(row["id"]),
        sensor_id=str(row["sensor_id"]),
        verb=row["verb"],
        payload=dict(row.get("payload") or {}),
        status=row["status"],
        delivery_count=int(row.get("delivery_count") or 0),
        last_delivered_at=row.get("last_delivered_at"),
        completed_at=row.get("completed_at"),
        expires_at=row["expires_at"],
        result=row.get("result"),
        created_at=row["created_at"],
    )


# ── Sites ────────────────────────────────────────────────────────────

@sites_router.get("", response_model=list[SiteResponse])
async def list_sites(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    rows = (await db.execute(text("""
        SELECT s.*, COALESCE(c.cnt, 0) AS sensor_count
        FROM sites s
        LEFT JOIN (
            SELECT site_id, COUNT(*) AS cnt FROM sensors WHERE site_id IS NOT NULL GROUP BY site_id
        ) c ON c.site_id = s.id
        ORDER BY s.name
    """))).mappings().all()
    return [
        SiteResponse(
            id=str(r["id"]),
            name=r["name"],
            region=r.get("region"),
            timezone=r.get("timezone") or "UTC",
            address=r.get("address"),
            notes=r.get("notes"),
            sensor_count=r.get("sensor_count", 0) or 0,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@sites_router.post("", response_model=SiteResponse, status_code=201)
async def create_site(
    data: SiteCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    row = (await db.execute(
        text("""
            INSERT INTO sites (name, region, timezone, address, notes)
            VALUES (:name, :region, :tz, :address, :notes)
            RETURNING *
        """),
        {
            "name": data.name, "region": data.region, "tz": data.timezone or "UTC",
            "address": data.address, "notes": data.notes,
        },
    )).mappings().first()
    await write_audit_log(
        db, actor=user, action="site.create", resource_type="site",
        resource_id=str(row["id"]), metadata={"name": row["name"]},
    )
    await db.commit()
    return SiteResponse(
        id=str(row["id"]), name=row["name"], region=row.get("region"),
        timezone=row.get("timezone") or "UTC", address=row.get("address"),
        notes=row.get("notes"), sensor_count=0,
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


@sites_router.put("/{site_id}", response_model=SiteResponse)
async def update_site(
    site_id: UUID,
    data: SiteUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(text("SELECT id FROM sites WHERE id = :id"), {"id": site_id})).first()
    if not existing:
        raise HTTPException(404, "Site not found")
    sets = ["updated_at = NOW()"]
    params: dict = {"id": site_id}
    for f in ("name", "region", "timezone", "address", "notes"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    await db.execute(text(f"UPDATE sites SET {', '.join(sets)} WHERE id = :id"), params)
    await write_audit_log(
        db, actor=user, action="site.update", resource_type="site",
        resource_id=str(site_id), metadata={"fields": sorted(params.keys() - {"id"})},
    )
    await db.commit()
    return await _get_site(site_id, db)


async def _get_site(site_id: UUID, db: AsyncSession) -> SiteResponse:
    r = (await db.execute(text("""
        SELECT s.*, COALESCE(c.cnt, 0) AS sensor_count
        FROM sites s
        LEFT JOIN (SELECT site_id, COUNT(*) AS cnt FROM sensors WHERE site_id IS NOT NULL GROUP BY site_id) c
            ON c.site_id = s.id
        WHERE s.id = :id
    """), {"id": site_id})).mappings().first()
    if not r:
        raise HTTPException(404, "Site not found")
    return SiteResponse(
        id=str(r["id"]), name=r["name"], region=r.get("region"),
        timezone=r.get("timezone") or "UTC", address=r.get("address"),
        notes=r.get("notes"), sensor_count=r.get("sensor_count", 0) or 0,
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


@sites_router.delete("/{site_id}", status_code=204)
async def delete_site(
    site_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(
        text("SELECT name FROM sites WHERE id = :id"), {"id": site_id}
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Site not found")
    sensor_count = (await db.execute(
        text("SELECT COUNT(*) FROM sensors WHERE site_id = :id"), {"id": site_id}
    )).scalar_one()
    if sensor_count:
        raise HTTPException(409, "Move or delete sensors assigned to this site before deleting it")
    await db.execute(text("DELETE FROM sites WHERE id = :id"), {"id": site_id})
    await write_audit_log(
        db, actor=user, action="site.delete", resource_type="site",
        resource_id=str(site_id), metadata={"name": existing["name"]},
    )
    await db.commit()


# ── Sensors: list / detail ───────────────────────────────────────────

@router.get("", response_model=list[SensorResponse])
async def list_sensors(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    rows = (await db.execute(text(f"{_SENSOR_LIST_SQL} ORDER BY s.name"))).mappings().all()
    return [_row_to_sensor(dict(r)) for r in rows]


@router.get("/{sensor_id}", response_model=SensorResponse)
async def get_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    r = (await db.execute(text(f"{_SENSOR_LIST_SQL} WHERE s.id = :id"), {"id": sensor_id})).mappings().first()
    if not r:
        raise HTTPException(404, "Sensor not found")
    return _row_to_sensor(dict(r))


@router.get("/{sensor_id}/downloads", response_model=SensorDownloadsResponse)
async def get_sensor_downloads(
    sensor_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    row = (await db.execute(
        text("SELECT id, name FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Sensor not found")

    server = _server_url(request)
    urls = _appliance_urls(server)
    artifact_dir = _latest_sensor_artifact_dir(sensor_id)
    if not artifact_dir:
        return SensorDownloadsResponse(
            sensor_id=str(sensor_id),
            sensor_name=row["name"],
            note=(
                "No configured OVA or seed ISO is stored for this sensor yet. "
                "Use Base OVA, or regenerate the enrollment token to create fresh configured artifacts."
            ),
            **urls,
        )

    download_token = artifact_dir.name
    configured_ova = artifact_dir / "zenplus-sensor-configured.ova"
    seed_iso = artifact_dir / "zenplus-sensor-seed.iso"
    updated_at = _artifact_mtime([configured_ova, seed_iso])
    return SensorDownloadsResponse(
        sensor_id=str(sensor_id),
        sensor_name=row["name"],
        configured_ova_url=(
            f"{server}/api/v1/sensors/bootstrap/{sensor_id}/{download_token}/configured.ova"
            if configured_ova.exists() and configured_ova.is_file() else None
        ),
        bootstrap_iso_url=(
            f"{server}/api/v1/sensors/bootstrap/{sensor_id}/{download_token}/seed.iso"
            if seed_iso.exists() and seed_iso.is_file() else None
        ),
        configured_ova_size_bytes=(
            configured_ova.stat().st_size if configured_ova.exists() and configured_ova.is_file() else None
        ),
        bootstrap_iso_size_bytes=(
            seed_iso.stat().st_size if seed_iso.exists() and seed_iso.is_file() else None
        ),
        artifact_token=download_token,
        updated_at=updated_at,
        **urls,
    )


@router.get("/{sensor_id}/events", response_model=list[SensorEventResponse])
async def list_sensor_events(
    sensor_id: UUID,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    limit = max(1, min(limit, 500))
    exists = (await db.execute(
        text("SELECT 1 FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).first()
    if not exists:
        raise HTTPException(404, "Sensor not found")
    rows = (await db.execute(
        text("""SELECT id, sensor_id, ts, kind, detail
                  FROM sensor_events
                 WHERE sensor_id = :id
                 ORDER BY ts DESC
                 LIMIT :limit"""),
        {"id": sensor_id, "limit": limit},
    )).mappings().all()
    return [
        SensorEventResponse(
            id=str(row["id"]),
            sensor_id=str(row["sensor_id"]),
            ts=row["ts"],
            kind=row["kind"],
            detail=dict(row.get("detail") or {}),
        )
        for row in rows
    ]


@router.get("/{sensor_id}/commands", response_model=list[SensorCommandResponse])
async def list_sensor_commands(
    sensor_id: UUID,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    limit = max(1, min(limit, 500))
    if not (await db.execute(
        text("SELECT 1 FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).first():
        raise HTTPException(404, "Sensor not found")
    rows = (await db.execute(
        text("""SELECT id, sensor_id, verb, payload, status, delivery_count,
                       last_delivered_at, completed_at, expires_at, result, created_at
                  FROM sensor_commands
                 WHERE sensor_id = :id
                 ORDER BY created_at DESC
                 LIMIT :limit"""),
        {"id": sensor_id, "limit": limit},
    )).mappings().all()
    return [_row_to_command(dict(row)) for row in rows]


@router.post("/{sensor_id}/commands", response_model=SensorCommandResponse, status_code=201)
async def queue_sensor_command(
    sensor_id: UUID,
    data: SensorCommandCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    sensor = (await db.execute(
        text("SELECT id, name, api_key_hash, status FROM sensors WHERE id = :id FOR UPDATE"),
        {"id": sensor_id},
    )).mappings().first()
    if not sensor:
        raise HTTPException(404, "Sensor not found")
    if not sensor.get("api_key_hash"):
        raise HTTPException(409, "Sensor must enroll before commands can be queued")
    if sensor["status"] == "disabled":
        raise HTTPException(409, "Enable the sensor before queuing commands")

    await db.execute(
        text("""UPDATE sensor_commands
                   SET status = 'expired', completed_at = NOW(),
                       result = COALESCE(result, 'command expired before completion')
                 WHERE sensor_id = :sensor_id
                   AND status IN ('pending', 'delivered')
                   AND expires_at <= NOW()"""),
        {"sensor_id": sensor_id},
    )
    payload = dict(data.payload)
    if data.verb == "update":
        # Import lazily to keep the dashboard and runtime routers independent
        # during application startup while sharing the exact signature check.
        from app.api.v1.sensor_api import _signed_binary_metadata

        signed_metadata, signed_manifest, signature = _signed_binary_metadata("linux-amd64")
        published_version = str(signed_metadata.get("version") or "").strip()
        if not published_version or not signed_manifest or not signature:
            raise HTTPException(
                409,
                "No release-signed linux-amd64 sensor update is published",
            )
        requested_version = str(payload.get("version") or "").strip()
        if requested_version and requested_version != published_version:
            raise HTTPException(
                409,
                f"Requested version {requested_version} is not the published version {published_version}",
            )
        controller_url = _server_url(request)
        if not controller_url.lower().startswith("https://"):
            raise HTTPException(409, "Sensor self-update requires an HTTPS controller URL")
        payload["manifest_url"] = f"{controller_url}/api/v1/sensor/bin/linux-amd64/manifest.json"
        payload["version"] = published_version
    row = (await db.execute(
        text("""INSERT INTO sensor_commands (
                    sensor_id, verb, payload, expires_at, created_by
                ) VALUES (
                    :sensor_id, :verb, CAST(:payload AS jsonb),
                    NOW() + make_interval(secs => :expires_in), :created_by
                )
                ON CONFLICT DO NOTHING
                RETURNING id, sensor_id, verb, payload, status, delivery_count,
                          last_delivered_at, completed_at, expires_at, result, created_at"""),
        {
            "sensor_id": sensor_id,
            "verb": data.verb,
            "payload": json.dumps(payload),
            "expires_in": data.expires_in_seconds,
            "created_by": user.id,
        },
    )).mappings().first()
    if not row:
        await db.rollback()
        raise HTTPException(409, f"An active {data.verb} command is already queued")
    await write_audit_log(
        db,
        actor=user,
        action="sensor.command.queue",
        resource_type="sensor",
        resource_id=str(sensor_id),
        metadata={"name": sensor["name"], "verb": data.verb},
    )
    await db.commit()
    return _row_to_command(dict(row))


@router.get("/{sensor_id}/vantages", response_model=list[SensorVantageResponse])
async def list_sensor_vantages(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    if not (await db.execute(
        text("SELECT 1 FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).first():
        raise HTTPException(404, "Sensor not found")
    rows = (await db.execute(text("""
        SELECT v.service_check_id::text, sc.name AS service_check_name,
               sc.check_type, v.state, v.last_change_at, v.last_result_at,
               v.last_latency_ms, v.last_error, v.last_tls_days_remaining
          FROM service_check_vantage_status v
          JOIN service_checks sc ON sc.id = v.service_check_id
         WHERE v.poller_id = :poller_id
         ORDER BY sc.name
    """), {"poller_id": str(sensor_id)})).mappings().all()
    return [
        SensorVantageResponse(
            service_check_id=row["service_check_id"],
            service_check_name=row["service_check_name"],
            check_type=row["check_type"],
            state=row["state"],
            last_change_at=row["last_change_at"],
            last_result_at=row["last_result_at"],
            last_latency_ms=row.get("last_latency_ms"),
            last_error=row.get("last_error"),
            tls_days_remaining=row.get("last_tls_days_remaining"),
        )
        for row in rows
    ]


# ── Sensors: create / update / delete ────────────────────────────────

@router.post("", status_code=201)
async def create_sensor(
    data: SensorCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    """Create a sensor record AND issue a one-time enrollment token.

    Returns both the sensor row and the (plaintext) enrollment token + a
    one-line install command. The token is shown ONCE — the dashboard should
    display it prominently and warn that it can't be retrieved later.
    """
    server = data.controller_url or _server_url(request)
    if not server.lower().startswith("https://"):
        raise HTTPException(
            503,
            "Remote sensors require an HTTPS controller URL; configure APP_BASE_URL or controller_url",
        )
    token, token_hash = _new_enrollment_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=ENROLLMENT_TTL_HOURS)
    _bootstrap_network_config(data)

    try:
        row = (await db.execute(
            text("""
                INSERT INTO sensors (
                    name, description, site_id, location, tags,
                    enrollment_token_hash, enrollment_expires_at,
                    status, min_supported_version, bootstrap_config, created_by
                ) VALUES (
                    :name, :desc, :site_id, :loc, CAST(:tags AS jsonb),
                    :tok, :exp, 'pending', :minimum_version,
                    CAST(:bootstrap_config AS jsonb), :uid
                )
                RETURNING *
            """),
            {
                "name": data.name, "desc": data.description,
                "site_id": data.site_id, "loc": data.location,
                "tags": _json_dumps(data.tags),
                "tok": token_hash, "exp": expires, "uid": user.id,
                "minimum_version": MIN_SUPPORTED_SENSOR_VERSION,
                "bootstrap_config": json.dumps(_nonsecret_bootstrap_config(data)),
            },
        )).mappings().first()
        await db.execute(
            text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                    VALUES (:id, 'created', CAST(:detail AS jsonb))"""),
            {
                "id": row["id"],
                "detail": json.dumps({"name": data.name, "site_id": str(data.site_id) if data.site_id else None}),
            },
        )
        await write_audit_log(
            db,
            actor=user,
            action="sensor.create",
            resource_type="sensor",
            resource_id=str(row["id"]),
            metadata={"name": data.name, "enrollment_expires_at": expires.isoformat()},
        )
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"A sensor named '{data.name}' already exists")
        raise
    await db.commit()

    sensor = await get_sensor(row["id"], db, user)
    return {
        "sensor": sensor,
        "token": await _token_response(row["id"], token, expires, server, data.name, data),
    }


@router.get("/bootstrap/{sensor_id}/{download_token}/seed.iso")
async def download_sensor_bootstrap_iso(
    sensor_id: UUID,
    download_token: str,
    db: AsyncSession = Depends(get_db),
):
    await _require_bootstrap_access(sensor_id, db)
    if len(download_token) < 20 or "/" in download_token:
        raise HTTPException(404, "Bootstrap ISO not found")
    path = BOOTSTRAP_DIR / str(sensor_id) / download_token / "zenplus-sensor-seed.iso"
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Bootstrap ISO not found or expired")
    return FileResponse(path, media_type="application/x-iso9660-image", filename=f"zenplus-sensor-{sensor_id}-seed.iso")


@router.get("/bootstrap/{sensor_id}/{download_token}/configured.ova")
async def download_configured_sensor_ova(
    sensor_id: UUID,
    download_token: str,
    db: AsyncSession = Depends(get_db),
):
    await _require_bootstrap_access(sensor_id, db)
    if len(download_token) < 20 or "/" in download_token:
        raise HTTPException(404, "Configured OVA not found")
    path = BOOTSTRAP_DIR / str(sensor_id) / download_token / "zenplus-sensor-configured.ova"
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Configured OVA not found or still generating")
    return FileResponse(
        path,
        media_type="application/x-virtualbox-ova",
        filename=f"zenplus-sensor-{sensor_id}-configured.ova",
    )


@router.put("/{sensor_id}", response_model=SensorResponse)
async def update_sensor(
    sensor_id: UUID,
    data: SensorUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    sets = ["updated_at = NOW()"]
    params: dict = {"id": sensor_id}
    update_data = data.model_dump(exclude_unset=True)
    if update_data.get("site_id") is not None:
        site = (await db.execute(text("SELECT id FROM sites WHERE id=:id"), {"id": update_data["site_id"]})).first()
        if not site:
            raise HTTPException(400, "Unknown site")
    for f in ("name", "description", "site_id", "location"):
        if f in update_data:
            sets.append(f"{f} = :{f}")
            params[f] = update_data[f]
    if "tags" in update_data:
        sets.append("tags = CAST(:tags AS jsonb)")
        params["tags"] = _json_dumps(update_data["tags"] or [])

    await db.execute(text(f"UPDATE sensors SET {', '.join(sets)} WHERE id = :id"), params)
    await write_audit_log(
        db, actor=user, action="sensor.update", resource_type="sensor",
        resource_id=str(sensor_id), metadata={"fields": sorted(update_data.keys())},
    )
    await db.commit()
    return await get_sensor(sensor_id, db, user)


@router.delete("/{sensor_id}", status_code=204)
async def delete_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(
        text("SELECT name FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Sensor not found")
    await db.execute(text("DELETE FROM sensors WHERE id = :id"), {"id": sensor_id})
    await write_audit_log(
        db, actor=user, action="sensor.delete", resource_type="sensor",
        resource_id=str(sensor_id), metadata={"name": existing["name"]},
    )
    await db.commit()
    await asyncio.to_thread(purge_sensor_bootstrap_artifacts, sensor_id)


# ── Sensors: token / key / status ────────────────────────────────────

@router.post("/{sensor_id}/regenerate-token", response_model=SensorTokenResponse)
async def regenerate_token(
    sensor_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(
        text("SELECT id, name, bootstrap_config FROM sensors WHERE id = :id"),
        {"id": sensor_id},
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    create_data = SensorCreate(
        name=existing["name"], **dict(existing.get("bootstrap_config") or {})
    )
    server = create_data.controller_url or _server_url(request)
    if not server.lower().startswith("https://"):
        raise HTTPException(
            503,
            "Remote sensors require an HTTPS controller URL; configure APP_BASE_URL or controller_url",
        )

    token, token_hash = _new_enrollment_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=ENROLLMENT_TTL_HOURS)
    await db.execute(
        text("""UPDATE sensors SET enrollment_token_hash = :tok,
                enrollment_expires_at = :exp,
                enrollment_consumed_at = NULL,
                enrollment_consumed_ip = NULL,
                updated_at = NOW()
                WHERE id = :id"""),
        {"tok": token_hash, "exp": expires, "id": sensor_id},
    )
    await write_audit_log(
        db,
        actor=user,
        action="sensor.enrollment_token.regenerate",
        resource_type="sensor",
        resource_id=str(sensor_id),
        metadata={"expires_at": expires.isoformat()},
    )
    await db.commit()
    await asyncio.to_thread(purge_sensor_bootstrap_artifacts, sensor_id)

    return await _token_response(
        sensor_id, token, expires, server, existing["name"], create_data
    )


@router.post("/{sensor_id}/rotate-key", response_model=SensorRotateKeyResponse)
async def rotate_key(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    raise HTTPException(
        409,
        "In-place sensor key rotation is unavailable because it would invalidate "
        "the running sensor. Regenerate an enrollment token and reprovision the "
        "sensor VM instead.",
    )


@router.post("/{sensor_id}/authorize", response_model=SensorResponse)
async def authorize_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    row = (await db.execute(text("""UPDATE sensors
        SET bootstrap_config = COALESCE(bootstrap_config, '{}'::jsonb) || jsonb_build_object('authorization_pending', false),
            status = 'online', status_reason = NULL, updated_at = NOW()
        WHERE id = :id AND api_key_hash IS NOT NULL AND status != 'disabled'
        RETURNING id"""), {"id": sensor_id})).first()
    if not row:
        raise HTTPException(409, "Sensor must be enrolled and enabled before authorization")
    await write_audit_log(db, actor=user, action="sensor.authorize", resource_type="sensor", resource_id=str(sensor_id))
    await db.commit()
    return await get_sensor(sensor_id, db, user)


@router.post("/{sensor_id}/disable", response_model=SensorResponse)
async def disable_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(
        text("SELECT name, status FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Sensor not found")
    await db.execute(
        text("""UPDATE sensors
                   SET status = 'disabled', status_reason = 'Disabled by administrator',
                       updated_at = NOW()
                 WHERE id = :id"""),
        {"id": sensor_id},
    )
    await db.execute(
        text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                VALUES (:id, 'status_changed', CAST(:detail AS jsonb))"""),
        {
            "id": sensor_id,
            "detail": json.dumps({"from": existing["status"], "to": "disabled", "reason": "administrator action"}),
        },
    )
    await resolve_sensor_alert(db, str(sensor_id), "sensor_degraded", "sensor disabled by administrator")
    await resolve_sensor_alert(db, str(sensor_id), "sensor_offline", "sensor disabled by administrator")
    await write_audit_log(
        db, actor=user, action="sensor.disable", resource_type="sensor",
        resource_id=str(sensor_id), metadata={"from": existing["status"]},
    )
    await db.commit()
    return await get_sensor(sensor_id, db, user)


@router.post("/{sensor_id}/enable", response_model=SensorResponse)
async def enable_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(
        text("SELECT status, api_key_hash FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Sensor not found")
    next_status = "pending" if existing.get("api_key_hash") is None else "offline"
    await db.execute(
        text("""UPDATE sensors SET
                  status = :status,
                  status_reason = :reason,
                  updated_at = NOW()
                WHERE id = :id"""),
        {
            "id": sensor_id,
            "status": next_status,
            "reason": "Awaiting enrollment" if next_status == "pending" else "Awaiting heartbeat after enable",
        },
    )
    await write_audit_log(
        db, actor=user, action="sensor.enable", resource_type="sensor",
        resource_id=str(sensor_id), metadata={"from": existing["status"], "to": next_status},
    )
    await db.execute(
        text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                VALUES (:id, 'status_changed', CAST(:detail AS jsonb))"""),
        {
            "id": sensor_id,
            "detail": json.dumps({"from": existing["status"], "to": next_status, "reason": "administrator action"}),
        },
    )
    await db.commit()
    return await get_sensor(sensor_id, db, user)


# ── Assignments ──────────────────────────────────────────────────────

@router.get("/{sensor_id}/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_VIEW),
):
    rows = (await db.execute(
        text("""
            SELECT a.*,
                   CASE
                     WHEN a.target_type = 'device'        THEN d.hostname
                     WHEN a.target_type = 'service_check' THEN sc.name
                     WHEN a.target_type = 'group'         THEN dg.name
                   END AS target_name
            FROM sensor_assignments a
            LEFT JOIN devices d         ON a.target_type='device' AND d.id = a.target_id
            LEFT JOIN service_checks sc ON a.target_type='service_check' AND sc.id = a.target_id
            LEFT JOIN device_groups dg  ON a.target_type='group' AND dg.id = a.target_id
            WHERE a.sensor_id = :id
            ORDER BY a.target_type, target_name NULLS LAST
        """),
        {"id": sensor_id},
    )).mappings().all()
    return [
        AssignmentResponse(
            sensor_id=str(r["sensor_id"]),
            target_type=r["target_type"],
            target_id=str(r["target_id"]),
            target_name=r.get("target_name"),
            priority=r.get("priority", 100) or 100,
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.put("/{sensor_id}/assignments")
async def replace_assignments(
    sensor_id: UUID,
    data: AssignmentBulk,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(SENSOR_MANAGE),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    target_tables = {
        "device": "devices",
        "service_check": "service_checks",
        "group": "device_groups",
    }
    missing: list[str] = []
    unsupported: list[str] = []
    for item in data.items:
        table = target_tables[item.target_type]
        if item.target_type == "service_check":
            found = (await db.execute(
                text("""SELECT id,
                               credential_id IS NULL
                               AND jsonb_array_length(
                                     COALESCE(workflow_steps, '[]'::jsonb)
                                   ) = 0 OR sensor_supports_service_auth((SELECT version FROM sensors WHERE id=:sensor_id)) AS remote_supported
                          FROM service_checks WHERE id = :id"""),
                {"id": item.target_id, "sensor_id": sensor_id},
            )).mappings().first()
            if found and not found["remote_supported"]:
                unsupported.append(str(item.target_id))
        else:
            found = (await db.execute(
                text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": item.target_id}
            )).first()
        if not found:
            missing.append(f"{item.target_type}:{item.target_id}")
    if missing:
        raise HTTPException(400, f"Unknown assignment target(s): {', '.join(missing)}")
    if unsupported:
        raise HTTPException(
            400,
            "Update this sensor to 1.23.5 or later for authenticated and workflow service checks: "
            + ", ".join(unsupported),
        )

    await db.execute(
        text("DELETE FROM sensor_assignments WHERE sensor_id = :id"),
        {"id": sensor_id},
    )
    for item in data.items:
        await db.execute(
            text("""INSERT INTO sensor_assignments (sensor_id, target_type, target_id, priority)
                    VALUES (:sid, :tt, :tid, :pri)
                    ON CONFLICT (sensor_id, target_type, target_id) DO UPDATE
                       SET priority = EXCLUDED.priority"""),
            {"sid": sensor_id, "tt": item.target_type, "tid": item.target_id, "pri": item.priority},
        )
    await write_audit_log(
        db,
        actor=user,
        action="sensor.assignments.replace",
        resource_type="sensor",
        resource_id=str(sensor_id),
        metadata={"assignment_count": len(data.items)},
    )
    await db.commit()
    return {"assigned": len(data.items)}


# ── tiny json helper, kept inline to avoid extra imports elsewhere ──

def _json_dumps(value) -> str:
    import json
    return json.dumps(value)


@router.get("/{sensor_id}/overview")
async def sensor_overview(sensor_id: UUID, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(SENSOR_VIEW)):
    from app.services.sensor_overview import assigned_targets, sensor_connection, sensor_measurements, target_results
    import logging
    row=(await db.execute(text(f"{_SENSOR_LIST_SQL} WHERE s.id=:id"),{'id':sensor_id})).mappings().first()
    if not row: raise HTTPException(404, 'Sensor not found')
    sensor=dict(row);now=datetime.now(timezone.utc)
    sensor['status']=sensor_connection(sensor,now)
    devices,services=await assigned_targets(db,sensor_id,user)
    available=True
    try:
        measured=await asyncio.to_thread(sensor_measurements,sensor_id,devices,services)
    except Exception:
        logging.getLogger(__name__).exception('Sensor overview measurements unavailable for %s',sensor_id)
        available=False;measured={}
    devices,services=target_results(devices,services,measured,sensor['status'],sensor.get('version'),now)
    from app.api.v1.sensor_api import _signed_binary_metadata, _binary_path
    release,manifest,signature=_signed_binary_metadata('linux-amd64')
    published={'version':release.get('version'), 'available':bool(manifest and signature and _binary_path('linux-amd64').is_file())}
    return {'sensor':_row_to_sensor(sensor), 'controller_url':(sensor.get('bootstrap_config') or {}).get('controller_url') or _server_url(request),
            'devices':devices,'services':services,'measurements_available':available,'observed_at':now,'release':published}
