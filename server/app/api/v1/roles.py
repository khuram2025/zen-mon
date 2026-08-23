"""Role management for RBAC.

Roles are rows in the ``roles`` table (migrate-074): a name, a display
name, and a JSONB array of permission ids from
``app.core.permissions.PERMISSION_MODULES``. Built-in roles
(``is_system``) cannot be renamed, edited, or deleted — duplicate them
into a custom role instead. ``users.role`` stores the role name by
value, so renaming a custom role propagates to its users in the same
transaction.
"""

import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.permissions import (
    SUPERUSER_PERMISSION,
    catalog,
    is_known_permission,
)
from app.core.security import (
    get_current_user,
    get_role_permissions,
    invalidate_role_cache,
    require_permission,
)
from app.models.role import Role
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/roles", tags=["Role Management"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


class RoleCreate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    display_name: str = Field(..., min_length=2, max_length=100)
    description: str = Field("", max_length=2000)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    display_name: Optional[str] = Field(None, min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    permissions: Optional[list[str]] = None


class RoleOut(BaseModel):
    id: UUID
    name: str
    display_name: str
    description: str
    permissions: list[str]
    is_system: bool
    user_count: int = 0

    model_config = {"from_attributes": True}


def _slugify(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")
    return slug[:50]


def _validate_permissions(perms: list[str]) -> list[str]:
    cleaned = sorted(set(perms))
    unknown = [p for p in cleaned if not is_known_permission(p)]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {', '.join(unknown)}")
    return cleaned


async def _guard_superuser_grant(db: AsyncSession, actor: User, perms: list[str]) -> None:
    """Only a full administrator may put system.admin on a role."""
    if SUPERUSER_PERMISSION in perms:
        actor_perms = await get_role_permissions(db, actor.role)
        if SUPERUSER_PERMISSION not in actor_perms:
            raise HTTPException(
                status_code=403,
                detail="Only a full administrator can grant the Full administration permission",
            )


async def _user_counts(db: AsyncSession) -> dict[str, int]:
    result = await db.execute(select(User.role, func.count(User.id)).group_by(User.role))
    return {row[0]: row[1] for row in result.all()}


@router.get("", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    counts = await _user_counts(db)
    result = await db.execute(select(Role).order_by(Role.is_system.desc(), Role.created_at))
    out = []
    for role in result.scalars().all():
        item = RoleOut.model_validate(role)
        item.user_count = counts.get(role.name, 0)
        out.append(item)
    return out


@router.get("/catalog")
async def permission_catalog(current_user: User = Depends(get_current_user)):
    """Permission vocabulary grouped by module, for the role editor."""
    return {"modules": catalog(), "superuser_permission": SUPERUSER_PERMISSION}


@router.post("", response_model=RoleOut, status_code=201)
async def create_role(
    data: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    name = (data.name or _slugify(data.display_name)).lower()
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Role name must be 2-50 chars: lowercase letters, digits, '_' or '-'",
        )
    perms = _validate_permissions(data.permissions)
    await _guard_superuser_grant(db, current_user, perms)

    existing = await db.execute(select(Role).where(Role.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"A role named '{name}' already exists")

    role = Role(
        name=name,
        display_name=data.display_name,
        description=data.description,
        permissions=perms,
        is_system=False,
    )
    db.add(role)
    await db.flush()
    await write_audit_log(
        db,
        actor=current_user,
        action="role.create",
        resource_type="role",
        resource_id=str(role.id),
        metadata={"name": role.name, "permissions": perms},
    )
    await db.commit()
    await db.refresh(role)
    invalidate_role_cache()
    return RoleOut.model_validate(role)


@router.put("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: UUID,
    data: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(
            status_code=400,
            detail="Built-in roles cannot be edited. Duplicate the role to customize it.",
        )

    changes: dict = {}

    if data.permissions is not None:
        perms = _validate_permissions(data.permissions)
        await _guard_superuser_grant(db, current_user, perms)
        role.permissions = perms
        changes["permissions"] = perms

    if data.name is not None and data.name.lower() != role.name:
        new_name = data.name.lower()
        if not _NAME_RE.match(new_name):
            raise HTTPException(
                status_code=400,
                detail="Role name must be 2-50 chars: lowercase letters, digits, '_' or '-'",
            )
        dup = await db.execute(select(Role).where(Role.name == new_name, Role.id != role_id))
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"A role named '{new_name}' already exists")
        # users.role stores the name by value — carry assignments along.
        await db.execute(update(User).where(User.role == role.name).values(role=new_name))
        changes["name"] = {"from": role.name, "to": new_name}
        role.name = new_name

    if data.display_name is not None:
        role.display_name = data.display_name
        changes["display_name"] = data.display_name

    if data.description is not None:
        role.description = data.description
        changes["description"] = data.description

    role.updated_at = datetime.now(timezone.utc)
    await write_audit_log(
        db,
        actor=current_user,
        action="role.update",
        resource_type="role",
        resource_id=str(role.id),
        metadata=changes,
    )
    await db.commit()
    await db.refresh(role)
    invalidate_role_cache()
    counts = await _user_counts(db)
    out = RoleOut.model_validate(role)
    out.user_count = counts.get(role.name, 0)
    return out


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("roles.manage")),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=400, detail="Built-in roles cannot be deleted")

    assigned = await db.execute(select(func.count(User.id)).where(User.role == role.name))
    count = assigned.scalar() or 0
    if count:
        raise HTTPException(
            status_code=409,
            detail=f"{count} user(s) still have this role. Reassign them first.",
        )

    deleted_name = role.name
    await db.delete(role)
    await write_audit_log(
        db,
        actor=current_user,
        action="role.delete",
        resource_type="role",
        resource_id=str(role_id),
        metadata={"name": deleted_name},
    )
    await db.commit()
    invalidate_role_cache()
