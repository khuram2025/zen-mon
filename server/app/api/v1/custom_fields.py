"""Custom fields API (``/api/v1/custom-fields``; docs/device-groups.md).

Field definitions: read with ``devices.view``; create, change and delete with
``devices.manage_policies`` and an unscoped user. Values: read with the asset's
view permission; set with ``devices.manage`` (devices) or ``servers.manage``
(servers) and the asset in the caller's scope. CSV import writes many assets at
once and needs ``devices.manage_policies`` (unscoped).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import SUPERUSER_PERMISSION
from app.core.security import get_current_user, get_role_permissions, require_permission
from app.models.user import User
from app.services import custom_fields as cf
from app.services.audit_service import write_audit_log
from app.services.device_groups import membership
from app.services.discovery_rules import conditions
from app.api.v1.device_groups import group_manager

router = APIRouter(prefix="/custom-fields", tags=["Custom fields"])


class FieldIn(BaseModel):
    key: Optional[str] = Field(default=None, max_length=50)
    label: Optional[str] = Field(default=None, max_length=80)
    field_type: Optional[str] = None
    choices: Optional[list[str]] = None
    asset_kinds: Optional[list[str]] = None
    description: Optional[str] = Field(default=None, max_length=500)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)
    show_in_list: Optional[bool] = None


class ValuesIn(BaseModel):
    asset_kind: str
    asset_id: uuid.UUID
    values: dict[str, Any]


class ImportIn(BaseModel):
    csv: str = Field(..., max_length=5_000_000)
    apply: bool = False


async def _can_write(db: AsyncSession, user: User, kind: str, asset_id: str) -> None:
    perms = await get_role_permissions(db, user.role)
    need = "devices.manage" if kind == "device" else "servers.manage"
    if SUPERUSER_PERMISSION not in perms and need not in perms:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    await _in_scope(db, user, kind, asset_id)


async def _in_scope(db: AsyncSession, user: User, kind: str, asset_id: str) -> None:
    table = "devices" if kind == "device" else "servers"
    row = (await db.execute(text(f"SELECT tags FROM {table} WHERE id = CAST(:id AS uuid)"), {"id": asset_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{kind.title()} not found")
    scope = await scoping.visible_tags(db, user)
    if scope is not None and not scoping.entity_visible(row[0], scope):
        raise HTTPException(status_code=404, detail=f"{kind.title()} not found")


def _uses_field(condition: Any, key: str) -> bool:
    if isinstance(condition, str):
        condition = json.loads(condition)
    return isinstance(condition, dict) and f"custom.{key}" in conditions.fields_used(condition)


@router.get("")
async def list_fields(db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("devices.view", "servers.view"))):
    return {"data": await cf.defs(db)}


@router.post("", status_code=201)
async def create_field(data: FieldIn, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    body = cf.validate_def(data.model_dump(exclude_unset=True))
    if await cf.def_by_key(db, body["key"]):
        raise HTTPException(status_code=409, detail="A custom field with this key already exists")
    await db.execute(text("""
        INSERT INTO custom_field_defs (key, label, field_type, choices, asset_kinds, description, sort_order, show_in_list, created_by)
        VALUES (:key, :label, :t, CAST(:choices AS jsonb), CAST(:ak AS text[]), :d, :s, :l, (SELECT id FROM users WHERE id = :u))
    """), {"key": body["key"], "label": body["label"], "t": body["field_type"],
           "choices": json.dumps(body["choices"]) if body.get("choices") else None, "ak": body["asset_kinds"],
           "d": body.get("description"), "s": body.get("sort_order", 100), "l": body.get("show_in_list", False),
           "u": getattr(user, "id", None)})
    await write_audit_log(db, actor=user, action="custom_field.create", resource_type="custom_field",
                          resource_id=body["key"], metadata=body)
    await db.commit()
    return await cf.def_by_key(db, body["key"])


@router.get("/values")
async def get_values(asset_kind: str = Query(..., pattern="^(device|server)$"), asset_id: uuid.UUID = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("devices.view", "servers.view"))):
    await _in_scope(db, user, asset_kind, str(asset_id))
    return {"values": await cf.values_for(db, asset_kind, str(asset_id))}


@router.put("/values")
async def put_values(data: ValuesIn, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("devices.manage", "servers.manage"))):
    if data.asset_kind not in ("device", "server"):
        raise HTTPException(status_code=422, detail="asset_kind must be device or server")
    await _can_write(db, user, data.asset_kind, str(data.asset_id))
    before = await cf.values_for(db, data.asset_kind, str(data.asset_id))
    after = await cf.set_values(db, data.asset_kind, str(data.asset_id), data.values, getattr(user, "id", None))
    await write_audit_log(db, actor=user, action="custom_field.set_values", resource_type=data.asset_kind,
                          resource_id=str(data.asset_id),
                          metadata={"changes": {k: [before.get(k), after.get(k)] for k in data.values}})
    await db.commit()
    return {"values": after}


@router.get("/list-values")
async def list_values(asset_kind: str = Query("device", pattern="^(device|server)$"),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("devices.view", "servers.view"))):
    """{asset_id: {key: value}} of the fields shown as list columns (Devices list)."""
    keys = [d["key"] for d in await cf.defs(db) if d["show_in_list"] and asset_kind in d["asset_kinds"]]
    if not keys:
        return {"fields": [], "values": {}}
    values = await cf.list_values(db, asset_kind, keys)
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        table = "devices" if asset_kind == "device" else "servers"
        visible = {i for i, tags in (await db.execute(text(f"SELECT id::text, tags FROM {table}"))).all()
                   if scoping.entity_visible(tags, scope)}
        values = {k: v for k, v in values.items() if k in visible}
    return {"fields": keys, "values": values}


@router.post("/import")
async def import_values(data: ImportIn, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    """CSV of values: first column ip_address, hostname or server, then one column per field key.
    Without ``apply`` it only reports what would change."""
    plan = await cf.plan_import(db, data.csv)
    if not data.apply:
        return {**plan, "applied": False}
    for row in plan["rows"]:
        await cf.set_values(db, row["asset_kind"], row["asset_id"], row["values"], getattr(user, "id", None))
    await write_audit_log(db, actor=user, action="custom_field.import", resource_type="custom_field",
                          metadata={"rows": len(plan["rows"]), "problems": len(plan["problems"]), "columns": plan["columns"]})
    await db.commit()
    await membership.reconcile(db, force=True)
    return {**plan, "applied": True}


# Per-field routes last: "/values", "/list-values" and "/import" must not be read as a key.
@router.put("/{key}")
async def update_field(key: str, data: FieldIn, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    existing = await cf.def_by_key(db, key)
    if existing is None:
        raise HTTPException(status_code=404, detail="Custom field not found")
    patch = data.model_dump(exclude_unset=True)
    if patch.get("key") not in (None, key) or patch.get("field_type") not in (None, existing["field_type"]):
        raise HTTPException(status_code=422, detail="A field's key and type cannot change; create a new field instead")
    body = cf.validate_def({k: v for k, v in patch.items() if k not in ("key", "field_type")}, existing=existing)
    if "asset_kinds" in body:
        dropped = [k for k in existing["asset_kinds"] if k not in body["asset_kinds"]]
        if dropped:
            n = (await db.execute(text("SELECT count(*) FROM custom_field_values WHERE field_id = CAST(:f AS uuid) "
                                       "AND asset_kind = ANY(CAST(:k AS text[]))"), {"f": existing["id"], "k": dropped})).scalar()
            if n:
                raise HTTPException(status_code=409, detail=f"{n} {dropped[0]} value(s) exist; clear them first")
    if body:
        sets, params = [], {"id": existing["id"]}
        for k, v in body.items():
            if k == "choices":
                sets.append("choices = CAST(:choices AS jsonb)"); params["choices"] = json.dumps(v)
            elif k == "asset_kinds":
                sets.append("asset_kinds = CAST(:ak AS text[])"); params["ak"] = v
            else:
                sets.append(f"{k} = :{k}"); params[k] = v
        await db.execute(text(f"UPDATE custom_field_defs SET {', '.join(sets)}, updated_at = now() WHERE id = CAST(:id AS uuid)"),
                         params)
        await write_audit_log(db, actor=user, action="custom_field.update", resource_type="custom_field",
                              resource_id=key, metadata=body)
        await db.commit()
    return await cf.def_by_key(db, key)


@router.delete("/{key}", status_code=204)
async def delete_field(key: str, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    existing = await cf.def_by_key(db, key)
    if existing is None:
        raise HTTPException(status_code=404, detail="Custom field not found")
    rows = (await db.execute(text("SELECT name, condition FROM device_groups WHERE kind = 'dynamic'"))).all()
    users = [name for name, cond in rows if _uses_field(cond, key)]
    if users:
        raise HTTPException(status_code=409, detail=f"Used by dynamic group(s): {', '.join(users)}")
    # Discovery rules test custom fields (custom.<key>) and set them ("Set custom field").
    rules = (await db.execute(text("SELECT name, condition, actions FROM discovery_engine_rules"))).all()
    rule_users = [name for name, cond, acts in rules
                  if _uses_field(cond, key) or any(isinstance(a, dict) and a.get("key") == "custom_field"
                                                   and (a.get("value") or {}).get("field") == key
                                                   for a in (json.loads(acts) if isinstance(acts, str) else acts or []))]
    if rule_users:
        raise HTTPException(status_code=409, detail=f"Used by discovery rule(s): {', '.join(rule_users)}")
    await db.execute(text("DELETE FROM custom_field_defs WHERE id = CAST(:id AS uuid)"), {"id": existing["id"]})
    await write_audit_log(db, actor=user, action="custom_field.delete", resource_type="custom_field",
                          resource_id=key, metadata={"label": existing["label"], "values_removed": existing["value_count"]})
    await db.commit()
