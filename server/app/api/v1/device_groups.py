"""Device groups API (``/api/v1/device-groups``; docs/device-groups.md).

Reading needs ``devices.view`` (router policy); a tag-scoped user sees only the
members in their scope. Creating, changing and deleting groups, and adding or
removing members explicitly, needs ``devices.manage_policies`` and an
unscoped user: groups scope alert rules and maintenance for everyone.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.device_groups import facts as gfacts
from app.services.device_groups import membership
from app.services.device_groups import service as svc
from app.services.discovery_rules import conditions

router = APIRouter(prefix="/device-groups", tags=["Device groups"])

MANAGE = "devices.manage_policies"


async def group_manager(user: User = Depends(require_permission(MANAGE)), db: AsyncSession = Depends(get_db)) -> User:
    if await scoping.visible_tags(db, user) is not None:
        raise HTTPException(status_code=403, detail="Groups scope alert rules and maintenance for every team; "
                                                    "a user limited to some devices cannot change them")
    return user


def _kinds(v: list[str]) -> list[str]:
    kinds = sorted(set(v or ["device"]))
    if any(k not in ("device", "server") for k in kinds):
        raise ValueError("asset_kinds: device and/or server")
    return kinds


class GroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    color: Optional[str] = Field(default=None, max_length=20)
    parent_id: Optional[uuid.UUID] = None
    kind: str = "static"
    condition: Optional[dict] = None
    asset_kinds: list[str] = Field(default_factory=lambda: ["device"])

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("static", "dynamic"):
            raise ValueError("kind must be static or dynamic")
        return v

    @field_validator("asset_kinds")
    @classmethod
    def _ak(cls, v: list[str]) -> list[str]:
        return _kinds(v)


class GroupPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    color: Optional[str] = Field(default=None, max_length=20)
    parent_id: Optional[uuid.UUID] = None
    condition: Optional[dict] = None
    asset_kinds: Optional[list[str]] = None

    @field_validator("asset_kinds")
    @classmethod
    def _ak(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return None if v is None else _kinds(v)


class PreviewIn(BaseModel):
    condition: dict
    asset_kinds: list[str] = Field(default_factory=lambda: ["device"])

    @field_validator("asset_kinds")
    @classmethod
    def _ak(cls, v: list[str]) -> list[str]:
        return _kinds(v)


class MembersIn(BaseModel):
    asset_kind: str = "device"
    asset_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=5000)

    @field_validator("asset_kind")
    @classmethod
    def _k(cls, v: str) -> str:
        if v not in ("device", "server"):
            raise ValueError("asset_kind must be device or server")
        return v


async def _audit(db, user, action, group, **meta):
    await write_audit_log(db, actor=user, action=f"device_group.{action}", resource_type="device_group",
                          resource_id=str(group["id"]) if group else None,
                          metadata={"name": (group or {}).get("name"), **meta})


async def _name_free(db: AsyncSession, name: str, exclude: Optional[str] = None) -> None:
    taken = (await db.execute(text(
        "SELECT 1 FROM device_groups WHERE lower(name) = lower(:n) AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))"),
        {"n": name.strip(), "x": exclude})).first()
    if taken:
        raise HTTPException(status_code=409, detail="A group with this name already exists")


# ── read ───────────────────────────────────────────────────────────────────

@router.get("")
async def list_groups(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return {"data": await svc.list_all(db)}


@router.get("/catalog")
async def catalog(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Fields and operators for the dynamic group condition builder (same shape as discovery rules)."""
    return {
        "fields": gfacts.catalog_payload(await gfacts.custom_defs(db)),
        "groups": [{"key": "all", "label": "All of"}, {"key": "any", "label": "Any of"}, {"key": "none", "label": "None of"}],
        "limits": {"max_depth": conditions.MAX_DEPTH, "max_leaves": conditions.MAX_LEAVES, "max_regex": conditions.MAX_REGEX},
    }


@router.post("/preview")
async def preview(data: PreviewIn, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Who a condition would include now. Nothing is saved."""
    try:
        return await membership.preview(db, data.condition, data.asset_kinds, scope=await scoping.visible_tags(db, user))
    except conditions.ConditionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{group_id}")
async def get_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    g = await svc.get_or_404(db, group_id)
    g["used_by"] = await svc.used_by(db, str(group_id))
    return g


@router.get("/{group_id}/members")
async def list_members(group_id: uuid.UUID, limit: int = Query(default=500, ge=1, le=5000),
                       db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await svc.get_or_404(db, group_id)
    rows = await svc.members(db, str(group_id), limit=limit)
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        rows = [r for r in rows if scoping.entity_visible(r.get("tags"), scope)]
    return {"data": rows}


# ── change ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_group(data: GroupIn, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    await _name_free(db, data.name)
    await svc.check_parent(db, None, str(data.parent_id) if data.parent_id else None)
    cond = None
    if data.kind == "dynamic":
        if data.condition is None:
            raise HTTPException(status_code=422, detail="A dynamic group needs a condition")
        cond = await svc.validate_condition(db, data.condition)
    elif data.condition is not None:
        raise HTTPException(status_code=422, detail="A static group has no condition")
    gid = (await db.execute(text("""
        INSERT INTO device_groups (name, description, color, parent_id, kind, condition, asset_kinds, created_by, updated_by)
        VALUES (:n, :d, :c, CAST(:p AS uuid), :k, CAST(:cond AS jsonb), CAST(:ak AS text[]),
                (SELECT id FROM users WHERE id = :u), (SELECT id FROM users WHERE id = :u))
        RETURNING id::text
    """), {"n": data.name.strip(), "d": data.description, "c": data.color,
           "p": str(data.parent_id) if data.parent_id else None, "k": data.kind,
           "cond": json.dumps(cond) if cond is not None else None, "ak": data.asset_kinds,
           "u": getattr(user, "id", None)})).scalar()
    g = await svc.get(db, gid)
    await _audit(db, user, "create", g, kind=data.kind, condition=cond)
    await db.commit()
    if data.kind == "dynamic":
        await membership.reconcile(db, group_ids=[gid], force=True)
    else:
        await membership.refresh_counts(db)
        await db.commit()
    return await svc.get(db, gid)


@router.put("/{group_id}")
async def update_group(group_id: uuid.UUID, data: GroupPatch, db: AsyncSession = Depends(get_db),
                       user: User = Depends(group_manager)):
    g = await svc.get_or_404(db, group_id)
    patch = data.model_dump(exclude_unset=True)
    sets, params = ["updated_at = now()", "updated_by = (SELECT id FROM users WHERE id = :u)"], {
        "id": str(group_id), "u": getattr(user, "id", None)}
    if "name" in patch and patch["name"]:
        await _name_free(db, patch["name"], str(group_id))
        sets.append("name = :n"); params["n"] = patch["name"].strip()
    if "description" in patch:
        sets.append("description = :d"); params["d"] = patch["description"]
    if "color" in patch:
        sets.append("color = :c"); params["c"] = patch["color"]
    if "parent_id" in patch:
        parent = str(patch["parent_id"]) if patch["parent_id"] else None
        await svc.check_parent(db, str(group_id), parent)
        sets.append("parent_id = CAST(:p AS uuid)"); params["p"] = parent
    if "condition" in patch:
        if g["kind"] != "dynamic":
            raise HTTPException(status_code=422, detail="A static group has no condition")
        cond = await svc.validate_condition(db, patch["condition"])
        sets.append("condition = CAST(:cond AS jsonb)"); params["cond"] = json.dumps(cond)
    if "asset_kinds" in patch and patch["asset_kinds"]:
        if g["kind"] == "static" and "server" in g["asset_kinds"] and "server" not in patch["asset_kinds"]:
            servers = (await db.execute(text("SELECT count(*) FROM device_group_members WHERE group_id = CAST(:g AS uuid) "
                                             "AND asset_kind = 'server'"), {"g": str(group_id)})).scalar()
            if servers:
                raise HTTPException(status_code=409, detail="Remove the group's servers first")
        sets.append("asset_kinds = CAST(:ak AS text[])"); params["ak"] = patch["asset_kinds"]
    await db.execute(text(f"UPDATE device_groups SET {', '.join(sets)} WHERE id = CAST(:id AS uuid)"), params)
    await _audit(db, user, "update", g, changes={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in patch.items()})
    await db.commit()
    if g["kind"] == "dynamic":
        await membership.reconcile(db, group_ids=[str(group_id)], force=True)
    else:
        await membership.refresh_counts(db)
        await db.commit()
    return await svc.get(db, group_id)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    g = await svc.get_or_404(db, group_id)
    reasons = svc.in_use(await svc.used_by(db, str(group_id)))
    if reasons:
        raise HTTPException(status_code=409, detail=f"The group is in use: {', '.join(reasons)}. "
                                                    "Change those first; deleting would remove its alert rules "
                                                    "and maintenance windows.")
    await _audit(db, user, "delete", g)
    await db.execute(text("DELETE FROM device_groups WHERE id = CAST(:id AS uuid)"), {"id": str(group_id)})
    await membership.refresh_counts(db)
    await db.commit()


@router.post("/{group_id}/members")
async def add_members(group_id: uuid.UUID, data: MembersIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(group_manager)):
    g = await svc.get_or_404(db, group_id)
    n = await svc.add_static(db, g, data.asset_kind, [str(i) for i in data.asset_ids])
    await _audit(db, user, "add_members", g, asset_kind=data.asset_kind, count=n)
    await membership.refresh_counts(db)
    await db.commit()
    return {"added": n}


@router.post("/{group_id}/members/remove")
async def remove_members(group_id: uuid.UUID, data: MembersIn, db: AsyncSession = Depends(get_db),
                         user: User = Depends(group_manager)):
    g = await svc.get_or_404(db, group_id)
    n = await svc.remove_static(db, g, data.asset_kind, [str(i) for i in data.asset_ids])
    await _audit(db, user, "remove_members", g, asset_kind=data.asset_kind, count=n)
    await membership.refresh_counts(db)
    await db.commit()
    return {"removed": n}


@router.post("/reconcile")
async def reconcile_now(db: AsyncSession = Depends(get_db), user: User = Depends(group_manager)):
    """Recompute every dynamic group now (also runs every minute)."""
    return await membership.reconcile(db, force=True)
