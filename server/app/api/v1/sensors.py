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
    POST   /api/v1/sensors/{id}/rotate-key  invalidate api key, return a new one
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

import hashlib
import json
import os
import re
import secrets
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
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.sensor import (
    SensorCreate,
    SensorUpdate,
    SensorResponse,
    SensorTokenResponse,
    SensorDownloadsResponse,
    SensorRotateKeyResponse,
    SiteCreate,
    SiteUpdate,
    SiteResponse,
    AssignmentBulk,
    AssignmentResponse,
)

router = APIRouter(prefix="/sensors", tags=["Sensors"])
sites_router = APIRouter(prefix="/sites", tags=["Sites"])


# ── Helpers ──────────────────────────────────────────────────────────

ENROLLMENT_TTL_HOURS = 24
TOKEN_PREFIX = "zps_"      # zenplus sensor
BOOTSTRAP_DIR = Path(os.getenv("ZENPLUS_SENSOR_BOOTSTRAP_DIR", "/opt/zenplus/artifacts/sensors/bootstrap"))


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
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _install_command(server_url: str, token: str, name: str) -> str:
    """A copy-pasteable one-liner the operator runs on the sensor VM.

    For Phase 1 testing, this points at the mock-sensor script we ship in
    /scripts. Once the real Go binary lands the only change is the URL.
    """
    return (
        f"curl -sSL {server_url}/api/v1/sensor/install.sh "
        f"| ZENPLUS_SERVER_URL='{server_url}' "
        f"ZENPLUS_ENROLLMENT_TOKEN='{token}' "
        f"ZENPLUS_SENSOR_NAME='{name}' bash"
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


def _sensor_env(server_url: str, token: str, name: str, proxy_url: Optional[str] = None) -> str:
    lines = [
        f"ZENPLUS_SERVER_URL={server_url}",
        f"ZENPLUS_ENROLLMENT_TOKEN={token}",
        f"ZENPLUS_SENSOR_NAME={name}",
        "ZENPLUS_VERIFY_TLS=1",
    ]
    if proxy_url:
        lines.extend([
            f"HTTP_PROXY={proxy_url}",
            f"HTTPS_PROXY={proxy_url}",
            "NO_PROXY=localhost,127.0.0.1,::1",
        ])
    return "\n".join(lines)


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
    password = json.dumps(data.console_password)
    return f"""
users:
  - default
  - name: {username}
    gecos: ZenPlus Sensor Administrator
    groups: [adm, sudo]
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: {password}
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
chpasswd:
  expire: false
"""


def _bootstrap_cloud_init(
    server_url: str,
    token: str,
    name: str,
    proxy_url: Optional[str] = None,
    create_data: Optional[SensorCreate] = None,
) -> str:
    """Cloud-init user-data for the production sensor appliance.

    The enrollment token is plaintext and shown only once, so this payload is
    returned only in create/regenerate responses. Operators can attach it as a
    NoCloud seed ISO or paste it into the first-boot wizard.
    """
    env = "\n".join(f"      {line}" for line in _sensor_env(server_url, token, name, proxy_url).splitlines())
    console_user = _console_user_cloud_init(create_data)
    return f"""#cloud-config
{console_user}ssh_pwauth: true
write_files:
  - path: /etc/zenplus-sensor/sensor.env
    owner: root:zenplus-sensor
    permissions: '0640'
    content: |
{env}
runcmd:
  - [ systemctl, enable, --now, zenplus-sensor.service ]
"""


def _bootstrap_meta_data(sensor_id: UUID, name: str) -> str:
    return f"""instance-id: zenplus-sensor-{sensor_id}
local-hostname: {name}
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


def _write_bootstrap_iso(
    sensor_id: UUID,
    download_token: str,
    user_data: str,
    meta_data: str,
    network_config: Optional[str],
) -> Optional[str]:
    cloud_localds = shutil.which("cloud-localds")
    if not cloud_localds:
        return None

    dest_dir = BOOTSTRAP_DIR / str(sensor_id) / download_token
    dest_dir.mkdir(parents=True, exist_ok=True)
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

    dest_dir = BOOTSTRAP_DIR / str(sensor_id) / download_token
    dest_dir.mkdir(parents=True, exist_ok=True)
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
        "console_username": (
            create_data.console_username
            if create_data and create_data.enable_console_user and create_data.console_username
            else "zenadmin"
        ),
        "console_password": (
            create_data.console_password
            if create_data and create_data.enable_console_user and create_data.console_password
            else "Read@123"
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
    ova_path = dest_dir / "zenplus-sensor-configured.ova"
    return download_token if ova_path.exists() else None


def _token_response(
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
    user_data = _bootstrap_cloud_init(controller_url, token, name, proxy_url, create_data)
    meta_data = _bootstrap_meta_data(sensor_id, name)
    network_config = _bootstrap_network_config(create_data) if create_data else None
    download_token = secrets.token_urlsafe(24)
    bootstrap_token = _write_bootstrap_iso(sensor_id, download_token, user_data, meta_data, network_config)
    configured_ova_token = _write_configured_ova(
        sensor_id, download_token, controller_url, token, name, create_data
    )
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
        install_command=_install_command(controller_url, token, name),
        bootstrap_cloud_init=user_data,
        bootstrap_meta_data=meta_data,
        bootstrap_network_config=network_config,
        bootstrap_iso_url=bootstrap_iso_url,
        configured_ova_url=configured_ova_url,
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
        id=str(r["id"]),
        name=r["name"],
        description=r.get("description"),
        site_id=str(r["site_id"]) if r.get("site_id") else None,
        site_name=r.get("site_name"),
        location=r.get("location"),
        status=r["status"],
        version=r.get("version"),
        last_seen_at=r.get("last_seen_at"),
        last_heartbeat_at=r.get("last_heartbeat_at"),
        last_ip=str(r["last_ip"]) if r.get("last_ip") else None,
        queue_depth=r.get("queue_depth", 0) or 0,
        queue_dropped_count=r.get("queue_dropped_count", 0) or 0,
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


# ── Sites ────────────────────────────────────────────────────────────

@sites_router.get("", response_model=list[SiteResponse])
async def list_sites(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
):
    await db.execute(text("DELETE FROM sites WHERE id = :id"), {"id": site_id})
    await db.commit()


# ── Sensors: list / detail ───────────────────────────────────────────

@router.get("", response_model=list[SensorResponse])
async def list_sensors(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(text(f"{_SENSOR_LIST_SQL} ORDER BY s.name"))).mappings().all()
    return [_row_to_sensor(dict(r)) for r in rows]


@router.get("/{sensor_id}", response_model=SensorResponse)
async def get_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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


# ── Sensors: create / update / delete ────────────────────────────────

@router.post("", status_code=201)
async def create_sensor(
    data: SensorCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a sensor record AND issue a one-time enrollment token.

    Returns both the sensor row and the (plaintext) enrollment token + a
    one-line install command. The token is shown ONCE — the dashboard should
    display it prominently and warn that it can't be retrieved later.
    """
    token, token_hash = _new_enrollment_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=ENROLLMENT_TTL_HOURS)
    _bootstrap_network_config(data)

    try:
        row = (await db.execute(
            text("""
                INSERT INTO sensors (
                    name, description, site_id, location, tags,
                    enrollment_token_hash, enrollment_expires_at,
                    status, created_by
                ) VALUES (
                    :name, :desc, :site_id, :loc, CAST(:tags AS jsonb),
                    :tok, :exp, 'pending', :uid
                )
                RETURNING *
            """),
            {
                "name": data.name, "desc": data.description,
                "site_id": data.site_id, "loc": data.location,
                "tags": _json_dumps(data.tags),
                "tok": token_hash, "exp": expires, "uid": user.id,
            },
        )).mappings().first()
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"A sensor named '{data.name}' already exists")
        raise
    await db.commit()

    server = _server_url(request)
    sensor = await get_sensor(row["id"], db, user)
    return {
        "sensor": sensor,
        "token": _token_response(row["id"], token, expires, server, data.name, data),
    }


@router.get("/bootstrap/{sensor_id}/{download_token}/seed.iso")
async def download_sensor_bootstrap_iso(sensor_id: UUID, download_token: str):
    if len(download_token) < 20 or "/" in download_token:
        raise HTTPException(404, "Bootstrap ISO not found")
    path = BOOTSTRAP_DIR / str(sensor_id) / download_token / "zenplus-sensor-seed.iso"
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Bootstrap ISO not found or expired")
    return FileResponse(path, media_type="application/x-iso9660-image", filename=f"zenplus-sensor-{sensor_id}-seed.iso")


@router.get("/bootstrap/{sensor_id}/{download_token}/configured.ova")
async def download_configured_sensor_ova(sensor_id: UUID, download_token: str):
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
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    sets = ["updated_at = NOW()"]
    params: dict = {"id": sensor_id}
    update_data = data.model_dump(exclude_unset=True)
    for f in ("name", "description", "site_id", "location", "status"):
        if f in update_data:
            sets.append(f"{f} = :{f}")
            params[f] = update_data[f]
    if "tags" in update_data:
        sets.append("tags = CAST(:tags AS jsonb)")
        params["tags"] = _json_dumps(update_data["tags"] or [])

    await db.execute(text(f"UPDATE sensors SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    return await get_sensor(sensor_id, db, user)


@router.delete("/{sensor_id}", status_code=204)
async def delete_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await db.execute(text("DELETE FROM sensors WHERE id = :id"), {"id": sensor_id})
    await db.commit()


# ── Sensors: token / key / status ────────────────────────────────────

@router.post("/{sensor_id}/regenerate-token", response_model=SensorTokenResponse)
async def regenerate_token(
    sensor_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        text("SELECT id, name FROM sensors WHERE id = :id"), {"id": sensor_id}
    )).mappings().first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

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
    await db.commit()

    server = _server_url(request)
    return _token_response(sensor_id, token, expires, server, existing["name"])


@router.post("/{sensor_id}/rotate-key", response_model=SensorRotateKeyResponse)
async def rotate_key(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

    api_key, api_hash, prefix = _new_api_key()
    now = datetime.now(timezone.utc)
    await db.execute(
        text("""UPDATE sensors SET api_key_hash = :h, api_key_prefix = :p,
                api_key_rotated_at = :now, updated_at = NOW()
                WHERE id = :id"""),
        {"h": api_hash, "p": prefix, "now": now, "id": sensor_id},
    )
    await db.commit()
    return SensorRotateKeyResponse(
        sensor_id=str(sensor_id), api_key=api_key, api_key_prefix=prefix, rotated_at=now,
    )


@router.post("/{sensor_id}/disable", response_model=SensorResponse)
async def disable_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await db.execute(
        text("UPDATE sensors SET status = 'disabled', updated_at = NOW() WHERE id = :id"),
        {"id": sensor_id},
    )
    await db.commit()
    return await get_sensor(sensor_id, db, user)


@router.post("/{sensor_id}/enable", response_model=SensorResponse)
async def enable_sensor(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await db.execute(
        text("""UPDATE sensors SET
                  status = CASE WHEN api_key_hash IS NULL THEN 'pending' ELSE 'offline' END,
                  updated_at = NOW()
                WHERE id = :id"""),
        {"id": sensor_id},
    )
    await db.commit()
    return await get_sensor(sensor_id, db, user)


# ── Assignments ──────────────────────────────────────────────────────

@router.get("/{sensor_id}/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    sensor_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(text("SELECT id FROM sensors WHERE id = :id"), {"id": sensor_id})).first()
    if not existing:
        raise HTTPException(404, "Sensor not found")

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
    await db.commit()
    return {"assigned": len(data.items)}


# ── tiny json helper, kept inline to avoid extra imports elsewhere ──

def _json_dumps(value) -> str:
    import json
    return json.dumps(value)
