"""Network Configuration Management (NCM) — E4 slice 1.

Versioned device configuration storage with content-hash de-duplication and a
unified diff between any two versions. Capture is manual / API in this slice
(SSH auto-fetch is a later slice). Per-device routes live under /devices/{id}/...
and the fleet overview under /ncm/overview.
"""
import hashlib
import difflib
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User

router = APIRouter(prefix="/ncm", tags=["NCM"])
device_router = APIRouter(prefix="/devices", tags=["NCM"])


class ConfigCapture(BaseModel):
    content: str = Field(..., min_length=1)
    config_type: str = Field(default="running", pattern="^(running|startup)$")
    source_note: Optional[str] = None
    captured_by: str = Field(default="manual", pattern="^(manual|api|ssh)$")


@device_router.post("/{device_id}/config-backup", status_code=201)
async def capture_config(
    device_id: UUID,
    data: ConfigCapture,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Store a config snapshot. De-duplicates: an identical capture (same hash
    as the latest of this type) does not create a new version."""
    dev = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")

    content = data.content.replace("\r\n", "\n")
    chash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    latest = (await db.execute(
        text("""
            SELECT id, content_hash FROM device_configs
            WHERE device_id = :d AND config_type = :t
            ORDER BY captured_at DESC LIMIT 1
        """),
        {"d": device_id, "t": data.config_type},
    )).first()
    if latest and latest.content_hash == chash:
        return {"is_change": False, "version_id": str(latest.id),
                "message": "No change since last backup"}

    row = (await db.execute(
        text("""
            INSERT INTO device_configs
                (device_id, config_type, content, content_hash, size_bytes, line_count, captured_by, source_note)
            VALUES (:d, :t, :c, :h, :sz, :lc, :by, :note)
            RETURNING id, captured_at
        """),
        {"d": device_id, "t": data.config_type, "c": content, "h": chash,
         "sz": len(content.encode("utf-8")), "lc": content.count("\n") + 1,
         "by": data.captured_by, "note": data.source_note},
    )).first()
    await db.commit()
    return {"is_change": True, "version_id": str(row.id),
            "captured_at": row.captured_at.isoformat(), "config_type": data.config_type}


@device_router.get("/{device_id}/configs")
async def list_configs(
    device_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""
            SELECT id, config_type, content_hash, size_bytes, line_count,
                   captured_at, captured_by, source_note
            FROM device_configs WHERE device_id = :d
            ORDER BY captured_at DESC LIMIT :lim
        """),
        {"d": device_id, "lim": limit},
    )).fetchall()
    return {
        "data": [{
            "id": str(r.id),
            "config_type": r.config_type,
            "hash": r.content_hash[:12],
            "size_bytes": r.size_bytes,
            "line_count": r.line_count,
            "captured_at": r.captured_at.isoformat(),
            "captured_by": r.captured_by,
            "source_note": r.source_note,
        } for r in rows],
        "count": len(rows),
    }


@device_router.get("/{device_id}/configs/{version_id}")
async def get_config(
    device_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    r = (await db.execute(
        text("""
            SELECT id, config_type, content, size_bytes, line_count,
                   captured_at, captured_by, source_note
            FROM device_configs WHERE id = :v AND device_id = :d
        """),
        {"v": version_id, "d": device_id},
    )).first()
    if not r:
        raise HTTPException(status_code=404, detail="Config version not found")
    return {
        "id": str(r.id), "config_type": r.config_type, "content": r.content,
        "size_bytes": r.size_bytes, "line_count": r.line_count,
        "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
        "source_note": r.source_note,
    }


@device_router.get("/{device_id}/configs-diff")
async def diff_configs(
    device_id: UUID,
    a: UUID,
    b: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Unified diff between two versions (a = older/base, b = newer)."""
    rows = (await db.execute(
        text("SELECT id, content, captured_at FROM device_configs WHERE device_id = :d AND id IN (:a, :b)"),
        {"d": device_id, "a": a, "b": b},
    )).fetchall()
    by_id = {str(r.id): r for r in rows}
    if str(a) not in by_id or str(b) not in by_id:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    ra, rb = by_id[str(a)], by_id[str(b)]
    diff = list(difflib.unified_diff(
        ra.content.splitlines(), rb.content.splitlines(),
        fromfile=f"{str(a)[:8]} ({ra.captured_at.isoformat()})",
        tofile=f"{str(b)[:8]} ({rb.captured_at.isoformat()})",
        lineterm="",
    ))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return {"diff": "\n".join(diff), "added": added, "removed": removed, "identical": len(diff) == 0}


@router.get("/overview")
async def ncm_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fleet-wide config-backup status: which devices have backups, how many
    versions, and when they were last captured."""
    rows = (await db.execute(
        text("""
            SELECT d.id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor,
                   c.versions, c.last_capture, c.last_by
            FROM devices d
            LEFT JOIN (
                SELECT device_id, count(*) AS versions, max(captured_at) AS last_capture,
                       (array_agg(captured_by ORDER BY captured_at DESC))[1] AS last_by
                FROM device_configs GROUP BY device_id
            ) c ON c.device_id = d.id
            ORDER BY (c.last_capture IS NULL), c.last_capture DESC NULLS LAST, d.hostname
        """)
    )).fetchall()

    data = []
    backed_up = 0
    for r in rows:
        if r.versions:
            backed_up += 1
        data.append({
            "device_id": str(r.id), "hostname": r.hostname, "ip": r.ip,
            "device_type": r.device_type, "vendor": r.vendor,
            "versions": r.versions or 0,
            "last_capture": r.last_capture.isoformat() if r.last_capture else None,
            "last_by": r.last_by,
        })
    return {"data": data, "total_devices": len(rows), "backed_up": backed_up}
