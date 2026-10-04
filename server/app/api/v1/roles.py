"""Role management for RBAC.

Roles are rows in the ``roles`` table (migrate-074): a name, a display
name, and a JSONB array of permission ids from
``app.core.permissions.PERMISSION_MODULES``. Built-in roles
(``is_system``) cannot be renamed, edited, or deleted — duplicate them
into a custom role instead.

Authorization is decided by the permissions a role grants, never by its name.
``users.role`` references ``roles.name`` through a foreign key with
``ON UPDATE CASCADE`` (migrate-150), so a rename carries assignments along
atomically and deleting an assigned role is refused by the database itself.

Delegation: a role manager can only grant permissions they hold, and can only
change or delete roles that are within their own access (see
``app.core.delegation``). Every change is versioned (``roles.revision``),
compare-and-set via ``expected_revision``, and recorded with a before/after
permission diff in ``role_history``.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import admin_guard, delegation
from app.core.database import get_db
from app.core.permissions import (
    ACCESS_CONTROL_PERMISSIONS,
    RESERVED_ROLE_NAMES,
    SUPERUSER_PERMISSION,
    catalog,
    effective_permissions,
    is_known_permission,
    risk_of,
)
from app.core.security import (
    get_current_user,
    invalidate_role_cache,
    require_permission,
)
from app.models.role import Role
from app.models.user import User
from app.services import role_references
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/roles", tags=["Role Management"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")
_READ_PERMS = ("roles.view", "roles.manage", "users.view", "users.manage")
_NAME_RULE = "Role identifier must be 2-50 chars: lowercase letters, digits, '_' or '-'"


def _clean_display_name(value: str) -> str:
    v = re.sub(r"\s+", " ", (value or "").strip())
    if not 2 <= len(v) <= 100:
        raise HTTPException(status_code=422, detail="Display name must be 2-100 characters")
    return v


class RoleCreate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    display_name: str = Field(..., max_length=100)
    description: str = Field("", max_length=2000)
    permissions: list[str] = Field(default_factory=list)
    reason: Optional[str] = Field(None, max_length=500)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    display_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    permissions: Optional[list[str]] = None
    # Revision the editor loaded. When supplied, a stale save is refused (409)
    # instead of silently overwriting a concurrent change.
    expected_revision: Optional[int] = None
    reason: Optional[str] = Field(None, max_length=500)


class ImpactRequest(BaseModel):
    permissions: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    id: UUID
    name: str
    display_name: str
    description: str
    permissions: list[str]
    is_system: bool
    user_count: int = 0
    revision: int = 1
    privileged: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = {"from_attributes": True}


def _out(role: Role, user_count: int = 0) -> RoleOut:
    item = RoleOut.model_validate(role)
    item.user_count = user_count
    item.revision = role.revision or 1
    item.privileged = bool(effective_permissions(role.permissions) & ACCESS_CONTROL_PERMISSIONS)
    return item


def _slugify(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")
    return slug[:50]


def _validate_permissions(perms: list[str]) -> list[str]:
    cleaned = sorted(set(perms))
    unknown = [p for p in cleaned if not is_known_permission(p)]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {', '.join(unknown)}")
    return cleaned


def _check_name(name: str) -> str:
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=_NAME_RULE)
    if name in RESERVED_ROLE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is a reserved role identifier; choose a different one",
        )
    return name


async def _user_counts(db: AsyncSession) -> dict[str, int]:
    result = await db.execute(select(User.role, func.count(User.id)).group_by(User.role))
    return {row[0]: row[1] for row in result.all()}


async def _get_role(db: AsyncSession, role_id: UUID, *, lock: bool = False) -> Role:
    q = select(Role).where(Role.id == role_id)
    if lock:
        q = q.with_for_update()
    role = (await db.execute(q)).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


async def _record_history(db: AsyncSession, role: Role, action: str, actor: User, *,
                          before: list[str], after: list[str], affected: int,
                          reason: Optional[str], changes: dict) -> None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    await db.execute(
        text(
            "INSERT INTO role_history (role_id, role_name, revision, action, actor_id, actor_username, "
            "added, removed, permissions_after, affected_users, reason, changes) VALUES "
            "(:rid, :rname, :rev, :action, :aid, :aname, CAST(:added AS jsonb), CAST(:removed AS jsonb), "
            "CAST(:after AS jsonb), :affected, :reason, CAST(:changes AS jsonb))"
        ),
        {"rid": role.id, "rname": role.name, "rev": role.revision or 1, "action": action,
         "aid": actor.id, "aname": actor.username, "added": json.dumps(added),
         "removed": json.dumps(removed), "after": json.dumps(sorted(after)),
         "affected": affected, "reason": (reason or "").strip() or None,
         "changes": json.dumps(changes, default=str)},
    )


async def _members_count(db: AsyncSession, role_name: str) -> int:
    return int((await db.execute(select(func.count(User.id)).where(User.role == role_name))).scalar() or 0)


def _diff(before: list[str], after: list[str]) -> dict:
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))}


@router.get("", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(*_READ_PERMS)),
):
    counts = await _user_counts(db)
    result = await db.execute(select(Role).order_by(Role.is_system.desc(), Role.created_at))
    return [_out(role, counts.get(role.name, 0)) for role in result.scalars().all()]


@router.get("/catalog")
async def permission_catalog(current_user: User = Depends(require_permission(*_READ_PERMS))):
    """Permission vocabulary grouped by module, with risk class and the
    dependencies the editor offers, for the role editor."""
    return {"modules": catalog(), "superuser_permission": SUPERUSER_PERMISSION}


@router.get("/assignable")
async def assignable_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Roles the signed-in account may assign to a user (within its own access)."""
    roles = (await db.execute(select(Role).order_by(Role.is_system.desc(), Role.created_at))).scalars().all()
    allowed = await delegation.assignable_role_names(db, current_user, roles)
    return [{"name": r.name, "display_name": r.display_name, "description": r.description,
             "permission_count": len(effective_permissions(r.permissions)),
             "privileged": bool(effective_permissions(r.permissions) & ACCESS_CONTROL_PERMISSIONS)}
            for r in roles if r.name in allowed]


@router.get("/{role_id}/members")
async def role_members(
    role_id: UUID,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.view", "roles.manage", "users.view", "users.manage")),
):
    role = await _get_role(db, role_id)
    total = await _members_count(db, role.name)
    rows = (await db.execute(
        select(User).where(User.role == role.name).order_by(func.lower(User.username)).limit(limit)
    )).scalars().all()
    return {
        "role": role.name, "total": total,
        "data": [{
            "id": str(u.id), "username": u.username, "full_name": u.full_name, "email": u.email,
            "is_active": u.is_active, "auth_source": u.auth_source, "scope_tags": list(u.scope_tags or []),
            "last_login": u.last_login, "mfa_enabled": u.mfa_enabled,
        } for u in rows],
    }


@router.get("/{role_id}/dependencies")
async def role_dependencies(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(*_READ_PERMS)),
):
    role = await _get_role(db, role_id)
    return {
        "users": await _members_count(db, role.name),
        "provider_references": await role_references.provider_role_references(db, role.name),
    }


@router.get("/{role_id}/history")
async def role_history(
    role_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.view", "roles.manage", "audit.view")),
):
    """Who changed this role, when, which permissions were added or removed, and
    how many accounts it affected."""
    rows = (await db.execute(text(
        "SELECT revision, action, actor_username, occurred_at, added, removed, affected_users, reason, changes "
        "FROM role_history WHERE role_id = :rid ORDER BY occurred_at DESC, revision DESC LIMIT :lim"),
        {"rid": role_id, "lim": limit})).all()
    return {"data": [{"revision": r[0], "action": r[1], "actor": r[2], "at": r[3], "added": r[4],
                      "removed": r[5], "affected_users": r[6], "reason": r[7], "changes": r[8]} for r in rows]}


@router.post("/{role_id}/impact")
async def role_change_impact(
    role_id: UUID,
    data: ImpactRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    """Dry run of a permission change: what is added / removed, how risky it is,
    who is affected, which directory mappings point at the role, and whether it
    would remove the last administrator. Changes nothing."""
    role = await _get_role(db, role_id)
    new_perms = _validate_permissions(data.permissions)
    before = sorted(role.permissions or [])
    diff = _diff(before, new_perms)
    members = (await db.execute(
        select(User).where(User.role == role.name).order_by(func.lower(User.username)).limit(20)
    )).scalars().all()
    total = await _members_count(db, role.name)
    active_members = int((await db.execute(
        select(func.count(User.id)).where(User.role == role.name, User.is_active.is_(True)))).scalar() or 0)
    admin_names = await admin_guard.full_admin_role_names(db)
    would_lose_admin = (role.name in admin_names and SUPERUSER_PERMISSION not in new_perms
                        and await admin_guard.count_active_full_admins(db) - active_members <= 0
                        and active_members > 0)
    return {
        "added": diff["added"], "removed": diff["removed"],
        "privileged_added": [p for p in diff["added"] if risk_of(p) in ("privileged", "critical")],
        "sensitive_added": [p for p in diff["added"] if risk_of(p) == "sensitive"],
        "affected_users": total, "affected_active": active_members,
        "sample_users": [{"username": u.username, "is_active": u.is_active, "auth_source": u.auth_source,
                          "scope_tags": list(u.scope_tags or [])} for u in members],
        "provider_references": await role_references.provider_role_references(db, role.name),
        "would_remove_last_administrator": would_lose_admin,
        "grants_beyond_you": delegation_missing(await delegation.actor_permissions(db, current_user), new_perms),
    }


def delegation_missing(actor_perms, target_perms) -> list[str]:
    from app.core.permissions import exceeds
    return exceeds(target_perms, actor_perms)


@router.post("", response_model=RoleOut, status_code=201)
async def create_role(
    data: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    display_name = _clean_display_name(data.display_name)
    if data.name:
        name = _check_name(data.name)
    else:
        slug = _slugify(display_name)
        # Non-Latin display names (e.g. Arabic) produce no ASCII slug: generate a
        # valid identifier instead of failing; the display name keeps its script.
        name = _check_name(slug) if slug and len(slug) >= 2 else f"role_{secrets.token_hex(3)}"
    perms = _validate_permissions(data.permissions)
    await delegation.assert_can_grant(db, current_user, perms)

    if (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"A role with the identifier '{name}' already exists")

    role = Role(
        name=name,
        display_name=display_name,
        description=(data.description or "").strip(),
        permissions=perms,
        is_system=False,
        revision=1,
        updated_by=current_user.username,
    )
    db.add(role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"A role with the identifier '{name}' already exists")
    await _record_history(db, role, "create", current_user, before=[], after=perms, affected=0,
                          reason=data.reason, changes={"name": name, "display_name": display_name})
    await write_audit_log(
        db,
        actor=current_user,
        action="role.create",
        resource_type="role",
        resource_id=str(role.id),
        metadata={"name": role.name, "permissions": perms, "reason": data.reason,
                  "privileged": [p for p in perms if risk_of(p) in ("privileged", "critical")]},
    )
    await db.commit()
    await db.refresh(role)
    invalidate_role_cache()
    return _out(role)


@router.put("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: UUID,
    data: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    # Serialize with every administrator-affecting change, then lock the row.
    await admin_guard.lock_admin_invariant(db)
    admins_before = await admin_guard.count_active_full_admins(db)
    role = await _get_role(db, role_id, lock=True)
    if role.is_system:
        raise HTTPException(
            status_code=400,
            detail="Built-in roles cannot be edited. Duplicate the role to customize it.",
        )
    if data.expected_revision is not None and data.expected_revision != (role.revision or 1):
        raise HTTPException(status_code=409, detail={
            "code": "role_revision_conflict",
            "message": "This role was changed by someone else since you opened it. Reload to see the latest version.",
            "current": _out(role, await _members_count(db, role.name)).model_dump(mode="json"),
        })

    before = sorted(role.permissions or [])
    # The actor must be able to manage the role as it stands today - a delegated
    # manager cannot empty (or otherwise alter) a role more privileged than itself.
    await delegation.assert_can_manage_role_target(db, current_user, before)

    changes: dict = {}
    renamed_from = None

    if data.permissions is not None:
        perms = _validate_permissions(data.permissions)
        await delegation.assert_can_grant(db, current_user, perms)
        if perms != before:
            role.permissions = perms
            changes["permissions"] = _diff(before, perms)

    if data.name is not None and data.name.strip().lower() != role.name:
        new_name = _check_name(data.name)
        if (await db.execute(select(Role).where(Role.name == new_name, Role.id != role_id))).scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"A role with the identifier '{new_name}' already exists")
        renamed_from = role.name
        changes["name"] = {"from": role.name, "to": new_name}
        role.name = new_name  # users.role follows via ON UPDATE CASCADE

    if data.display_name is not None:
        dn = _clean_display_name(data.display_name)
        if dn != role.display_name:
            changes["display_name"] = {"from": role.display_name, "to": dn}
            role.display_name = dn

    if data.description is not None:
        desc = data.description.strip()
        if desc != (role.description or ""):
            changes["description"] = {"from": role.description, "to": desc}
            role.description = desc

    if not changes:
        return _out(role, await _members_count(db, role.name))

    role.revision = (role.revision or 1) + 1
    role.updated_at = datetime.now(timezone.utc)
    role.updated_by = current_user.username
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="That role identifier is already in use")
    migrated_refs = 0
    if renamed_from:
        # Belt and braces for installs where the FK has not been validated yet,
        # then carry directory mappings along in the same transaction.
        from sqlalchemy import update
        await db.execute(update(User).where(User.role == renamed_from).values(role=role.name))
        migrated_refs = await role_references.rewrite_provider_role_references(db, renamed_from, role.name)
        changes["directory_references_migrated"] = migrated_refs
    await admin_guard.assert_admin_survives(db, admins_before)

    affected = await _members_count(db, role.name)
    after = sorted(role.permissions or [])
    await _record_history(db, role, "rename" if renamed_from and len(changes) == 2 else "update", current_user,
                          before=before, after=after, affected=affected, reason=data.reason, changes=changes)
    await write_audit_log(
        db,
        actor=current_user,
        action="role.update",
        resource_type="role",
        resource_id=str(role.id),
        metadata={**changes, "revision": role.revision, "affected_users": affected, "reason": data.reason},
    )
    await db.commit()
    await db.refresh(role)
    invalidate_role_cache()
    return _out(role, await _members_count(db, role.name))


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    role = await _get_role(db, role_id, lock=True)
    if role.is_system:
        raise HTTPException(status_code=400, detail="Built-in roles cannot be deleted")
    await delegation.assert_can_manage_role_target(db, current_user, role.permissions or [])

    count = await _members_count(db, role.name)
    if count:
        raise HTTPException(
            status_code=409,
            detail=f"{count} user(s) still have this role. Reassign them first.",
        )
    refs = await role_references.provider_role_references(db, role.name)
    if refs:
        where = ", ".join(sorted({f"{r['provider_label']} ({r['kind'].replace('_', ' ')})" for r in refs}))
        raise HTTPException(
            status_code=409,
            detail=f"This role is used by directory authentication: {where}. Point those rules at another role first.",
        )

    deleted_name = role.name
    before = sorted(role.permissions or [])
    try:
        await _record_history(db, role, "delete", current_user, before=before, after=[], affected=0,
                              reason=None, changes={"name": deleted_name})
        await db.delete(role)
        await write_audit_log(
            db,
            actor=current_user,
            action="role.delete",
            resource_type="role",
            resource_id=str(role_id),
            metadata={"name": deleted_name, "permissions": before},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A user was assigned this role while you were deleting it")
    invalidate_role_cache()
