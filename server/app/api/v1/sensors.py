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
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
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
    install_cmd = _install_command(server, token, data.name)

    sensor = await get_sensor(row["id"], db, user)
    return {
        "sensor": sensor,
        "token": SensorTokenResponse(
            sensor_id=str(row["id"]),
            enrollment_token=token,
            expires_at=expires,
            server_url=server,
            install_command=install_cmd,
        ),
    }


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
    return SensorTokenResponse(
        sensor_id=str(sensor_id),
        enrollment_token=token,
        expires_at=expires,
        server_url=server,
        install_command=_install_command(server, token, existing["name"]),
    )


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
