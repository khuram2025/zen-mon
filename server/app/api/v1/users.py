from datetime import datetime, timezone
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, hash_password, require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/users", tags=["User Management"])


async def _send_account_notice(db: AsyncSession, user: User, *, subject: str,
                               title: str, message: str, changed_by: str) -> None:
    """Best-effort security notice to the affected user. Never raises: account
    changes must succeed even when no SMTP gateway is configured."""
    try:
        row = (await db.execute(text(
            "SELECT config FROM notification_gateways "
            "WHERE type = 'smtp' AND is_default = true LIMIT 1"
        ))).first()
        gw = dict(row.config) if row else None
        if not gw:
            row = (await db.execute(text(
                "SELECT value FROM system_settings WHERE key = 'smtp'"
            ))).first()
            gw = row[0] if row and isinstance(row[0], dict) else None
        if not gw or not gw.get("host") or not gw.get("enabled", True):
            return
        from app.api.v1.alert_engine import _send_email
        from app.services.email_render import (
            build_account_email_html, build_account_email_text,
        )
        ctx = {
            "title": title,
            "recipient_name": user.full_name or user.username,
            "message": message,
            "details": [("Account", user.username), ("Changed by", changed_by)],
        }
        await _send_email(gw, user.email, subject,
                          build_account_email_text(ctx),
                          html_body=build_account_email_html(ctx))
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "account notice email to %s failed", user.email, exc_info=True)


# ── Schemas ───────────────────────────────────────────────────────────────────

VALID_ROLES = ["admin", "operator", "viewer", "read_only"]

ROLE_DESCRIPTIONS = {
    "admin": "Full system access. Manage users, settings, devices, alerts, and all configurations.",
    "operator": "Manage devices, service checks, alerts. Cannot manage users or system settings.",
    "viewer": "View dashboards, devices, alerts. Can acknowledge alerts but cannot modify configurations.",
    "read_only": "Read-only access to dashboards and reports. No modification permissions.",
}

ROLE_PERMISSIONS = {
    "admin": [
        "users.manage", "settings.manage", "devices.manage", "devices.view",
        "alerts.manage", "alerts.acknowledge", "alerts.view",
        "service_checks.manage", "service_checks.view",
        "reports.view", "reports.export", "discovery.run",
    ],
    "operator": [
        "devices.manage", "devices.view",
        "alerts.manage", "alerts.acknowledge", "alerts.view",
        "service_checks.manage", "service_checks.view",
        "reports.view", "reports.export", "discovery.run",
    ],
    "viewer": [
        "devices.view", "alerts.acknowledge", "alerts.view",
        "service_checks.view", "reports.view",
    ],
    "read_only": [
        "devices.view", "alerts.view", "service_checks.view", "reports.view",
    ],
}


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    full_name: Optional[str] = Field(None, max_length=255)
    role: str = Field(default="viewer")
    is_active: bool = True


class UserUpdate(BaseModel):
    email: Optional[str] = Field(None, max_length=255)
    full_name: Optional[str] = Field(None, max_length=255)
    role: Optional[str] = None
    is_active: Optional[bool] = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


class UserOut(BaseModel):
    id: UUID
    username: str
    email: str
    full_name: Optional[str]
    role: str
    is_active: bool
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoleOut(BaseModel):
    id: str
    name: str
    description: str
    permissions: list[str]
    user_count: int = 0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all available roles with descriptions and permissions."""
    # Count users per role
    result = await db.execute(
        select(User.role, func.count(User.id)).group_by(User.role)
    )
    counts = {row[0]: row[1] for row in result.all()}

    roles = []
    for role_id, desc in ROLE_DESCRIPTIONS.items():
        roles.append(RoleOut(
            id=role_id,
            name=role_id.replace("_", " ").title(),
            description=desc,
            permissions=ROLE_PERMISSIONS.get(role_id, []),
            user_count=counts.get(role_id, 0),
        ))
    return roles


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """List all users. Requires admin role."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Create a new user. Requires admin role."""
    if data.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

    # Check duplicates
    existing = await db.execute(
        select(User).where((User.username == data.username) | (User.email == data.email))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username or email already exists")

    user = User(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        role=data.role,
        is_active=data.is_active,
    )
    db.add(user)
    await db.flush()
    await write_audit_log(
        db,
        actor=current_user,
        action="user.create",
        resource_type="user",
        resource_id=str(user.id),
        metadata={"username": user.username, "role": user.role, "is_active": user.is_active},
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut.model_validate(user)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Update user details. Requires admin role."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.role is not None:
        if data.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")
        # Prevent removing last admin
        if user.role == "admin" and data.role != "admin":
            admin_count = await db.execute(
                select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
            )
            if admin_count.scalar() <= 1:
                raise HTTPException(status_code=400, detail="Cannot remove the last admin user")
        user.role = data.role

    if data.email is not None:
        existing = await db.execute(select(User).where(User.email == data.email, User.id != user_id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already in use")
        user.email = data.email

    if data.full_name is not None:
        user.full_name = data.full_name

    if data.is_active is not None:
        # Prevent deactivating last admin
        if user.role == "admin" and not data.is_active:
            admin_count = await db.execute(
                select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
            )
            if admin_count.scalar() <= 1:
                raise HTTPException(status_code=400, detail="Cannot deactivate the last admin user")
        # Prevent self-deactivation
        if user.id == current_user.id and not data.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
        user.is_active = data.is_active

    user.updated_at = datetime.now(timezone.utc)
    await write_audit_log(
        db,
        actor=current_user,
        action="user.update",
        resource_type="user",
        resource_id=str(user.id),
        metadata=data.model_dump(exclude_unset=True),
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Delete a user. Requires admin role. Cannot delete yourself or last admin."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "admin":
        admin_count = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)
        )
        if admin_count.scalar() <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin user")

    deleted_username = user.username
    deleted_role = user.role
    await db.delete(user)
    await write_audit_log(
        db,
        actor=current_user,
        action="user.delete",
        resource_type="user",
        resource_id=str(user_id),
        metadata={"username": deleted_username, "role": deleted_role},
    )
    await db.commit()


@router.post("/{user_id}/reset-password", status_code=200)
async def reset_password(
    user_id: UUID,
    data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Reset a user's password. Requires admin role."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = hash_password(data.new_password)
    user.updated_at = datetime.now(timezone.utc)
    await write_audit_log(
        db,
        actor=current_user,
        action="user.password_reset",
        resource_type="user",
        resource_id=str(user.id),
        metadata={"username": user.username},
    )
    await db.commit()
    await _send_account_notice(
        db, user,
        subject="[ZenPlus] Your password was reset",
        title="Your password was reset",
        message="An administrator has reset the password for your ZenPlus "
                "account. Use the new password provided to you to sign in.",
        changed_by=f"{current_user.username} (administrator)",
    )
    return {"message": "Password reset successfully"}


@router.post("/me/change-password", status_code=200)
async def change_own_password(
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change your own password."""
    from app.core.security import verify_password
    if not verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(data.new_password)
    current_user.updated_at = datetime.now(timezone.utc)
    await write_audit_log(
        db,
        actor=current_user,
        action="user.password_change",
        resource_type="user",
        resource_id=str(current_user.id),
        metadata={"username": current_user.username},
    )
    await db.commit()
    await _send_account_notice(
        db, current_user,
        subject="[ZenPlus] Your password was changed",
        title="Your password was changed",
        message="The password for your ZenPlus account was just changed. "
                "Your sessions on other devices remain signed in.",
        changed_by="you",
    )
    return {"message": "Password changed successfully"}
