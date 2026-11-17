"""Device type catalogue (``/api/v1/device-types``; migrate-189).

``devices.device_type`` and ``device_profiles.device_type`` reference this
table. Anyone who can see devices can read it; adding, editing and deleting
types needs ``devices.manage_templates`` and an unscoped user. Built-in types
can be relabelled but not deleted; a type in use cannot be deleted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.device_templates import template_manager
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.snmp import DeviceTypeCreate, DeviceTypeUpdate
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/device-types", tags=["Device Types"])

_SELECT = """
    SELECT t.key, t.label, t.category, t.icon, t.sort_order, t.builtin, t.description,
           (SELECT count(*) FROM devices d WHERE d.device_type = t.key) AS device_count,
           (SELECT count(*) FROM device_profiles p WHERE p.device_type = t.key) AS template_count
      FROM device_types t
"""


async def _one(db: AsyncSession, key: str) -> dict:
    row = (await db.execute(text(_SELECT + " WHERE t.key = :k"), {"k": key})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Device type not found")
    return dict(row)


@router.get("")
async def list_types(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text(_SELECT + " ORDER BY t.sort_order, t.label"))).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.post("", status_code=201)
async def create_type(data: DeviceTypeCreate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(template_manager)):
    exists = (await db.execute(text("SELECT 1 FROM device_types WHERE key = :k"), {"k": data.key})).first()
    if exists:
        raise HTTPException(status_code=409, detail="A device type with this key already exists")
    await db.execute(text("""
        INSERT INTO device_types (key, label, category, icon, sort_order, builtin, description)
        VALUES (:key, :label, :category, :icon, :sort, FALSE, :desc)
    """), {"key": data.key, "label": data.label.strip(), "category": data.category, "icon": data.icon,
           "sort": data.sort_order, "desc": data.description})
    await write_audit_log(db, actor=user, action="device_type.create", resource_type="device_type",
                          resource_id=data.key, metadata=data.model_dump())
    await db.commit()
    return await _one(db, data.key)


@router.put("/{key}")
async def update_type(key: str, data: DeviceTypeUpdate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(template_manager)):
    before = await _one(db, key)
    patch = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if not patch:
        return before
    sets = ", ".join(f"{k} = :{k}" for k in patch)
    await db.execute(text(f"UPDATE device_types SET {sets}, updated_at = now() WHERE key = :key"),
                     {**patch, "key": key})
    await write_audit_log(db, actor=user, action="device_type.update", resource_type="device_type",
                          resource_id=key, metadata={"changes": {k: [before.get(k), v] for k, v in patch.items()}})
    await db.commit()
    return await _one(db, key)


@router.delete("/{key}", status_code=204)
async def delete_type(key: str, db: AsyncSession = Depends(get_db), user: User = Depends(template_manager)):
    row = await _one(db, key)
    if row["builtin"]:
        raise HTTPException(status_code=400, detail="Built-in device types cannot be deleted")
    if row["device_count"] or row["template_count"]:
        raise HTTPException(status_code=409, detail=f"In use by {row['device_count']} device(s) and "
                                                    f"{row['template_count']} template(s); change them first")
    await db.execute(text("DELETE FROM device_types WHERE key = :k"), {"k": key})
    await write_audit_log(db, actor=user, action="device_type.delete", resource_type="device_type",
                          resource_id=key, metadata={"label": row["label"]})
    await db.commit()
