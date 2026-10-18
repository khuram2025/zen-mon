from app.core.crypto import decrypt_config
import csv
import io
import logging
import re
from datetime import datetime, timezone
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, text, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import admin_guard, delegation, password_policy
from app.core.database import get_db
from app.core.permissions import (
    LEGACY_ROLE_PERMISSIONS,
    PERMISSION_MODULES,
    SUPERUSER_PERMISSION,
    effective_permissions,
    risk_of,
)
from app.core.security import get_current_user, hash_password, require_permission
from app.models.role import Role
from app.models.user import User
from app.services import session_service, tag_service
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/users", tags=["User Management"])
logger = logging.getLogger(__name__)


async def _send_account_notice(db: AsyncSession, user: User, *, subject: str,
                               title: str, message: str, changed_by: str) -> None:
    """Best-effort security notice to the affected user. Never raises: account
    changes must succeed even when no SMTP gateway is configured."""
    try:
        row = (await db.execute(text(
            "SELECT config FROM notification_gateways "
            "WHERE type = 'smtp' AND is_default = true LIMIT 1"
        ))).first()
        gw = decrypt_config(row.config) if row else None
        if not gw:
            row = (await db.execute(text(
                "SELECT value FROM system_settings WHERE key = 'smtp'"
            ))).first()
            gw = decrypt_config(row[0]) if row and isinstance(row[0], dict) else None
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
        logger.warning("account notice email to %s failed", user.email, exc_info=True)


# ── Role helpers ──────────────────────────────────────────────────────────────
# Roles live in the `roles` table (migrate-074). The legacy hardcoded
# vocabulary remains only as a fallback for installs mid-update.

LEGACY_VALID_ROLES = ["admin", "operator", "viewer", "read_only"]

LEGACY_ROLE_DESCRIPTIONS = {
    "admin": "Full system access. Manage users, settings, devices, alerts, and all configurations.",
    "operator": "Manage devices, service checks, alerts. Cannot manage users or system settings.",
    "viewer": "View dashboards, devices, alerts. Can acknowledge alerts but cannot modify configurations.",
    "read_only": "Read-only access to dashboards and reports. No modification permissions.",
}


async def _db_roles(db: AsyncSession) -> list[Role]:
    try:
        async with db.begin_nested():
            result = await db.execute(select(Role).order_by(Role.is_system.desc(), Role.created_at))
            return list(result.scalars().all())
    except Exception:
        return []


async def _role_by_name(db: AsyncSession, name: str) -> Optional[Role]:
    return next((r for r in await _db_roles(db) if r.name == name), None)


async def _valid_role_names(db: AsyncSession) -> list[str]:
    roles = await _db_roles(db)
    return [r.name for r in roles] if roles else LEGACY_VALID_ROLES


async def _role_permissions(db: AsyncSession, name: str) -> list[str]:
    role = await _role_by_name(db, name)
    if role is not None:
        return list(role.permissions or [])
    return list(LEGACY_ROLE_PERMISSIONS.get(name, [])) if not await _db_roles(db) else []


# ── Identity validation ───────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\\\"]+@[^@\s<>()\[\],;:\\\"]+\.[^@\s<>()\[\],;:\\\"]{2,}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_username(value: str) -> str:
    """Usernames are stored exactly as entered, minus surrounding whitespace
    (which login also strips). Uniqueness is case-insensitive."""
    v = (value or "").strip()
    if not 3 <= len(v) <= 100:
        raise ValueError("Username must be 3-100 characters")
    if _CONTROL_RE.search(v):
        raise ValueError("Username must not contain control characters")
    return v


def _clean_email(value: str) -> str:
    v = (value or "").strip().lower()
    if len(v) > 255 or not _EMAIL_RE.match(v):
        raise ValueError("Enter a valid email address")
    return v


def _clean_timezone(value: Optional[str]) -> Optional[str]:
    from app.services.oncall_service import valid_timezone
    v = (value or "").strip()
    if not v:
        return None
    if not valid_timezone(v):
        raise ValueError("Enter an IANA time zone such as Asia/Riyadh")
    return v


class UserCreate(BaseModel):
    username: str = Field(..., max_length=100)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=password_policy.MIN_LENGTH, max_length=password_policy.MAX_LENGTH)
    full_name: Optional[str] = Field(None, max_length=255)
    role: str = Field(default="viewer")
    is_active: bool = True
    # Visibility scope: tag names limiting what this user sees. Empty =
    # unrestricted. Canonicalized against the tag registry on write.
    scope_tags: list[str] = Field(default_factory=list)
    # Require a new password at first sign-in (recommended whenever an
    # administrator chooses the initial password).
    must_change_password: bool = False

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        return _clean_username(v)

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        return _clean_email(v)

    @field_validator("full_name")
    @classmethod
    def _v_full_name(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class UserUpdate(BaseModel):
    """Patch semantics: only fields present in the request are applied. An
    explicit ``null`` / empty ``full_name`` clears it; an omitted one preserves it."""
    email: Optional[str] = Field(None, max_length=255)
    full_name: Optional[str] = Field(None, max_length=255)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    scope_tags: Optional[list[str]] = None
    must_change_password: Optional[bool] = None
    # Personal IANA time zone; explicit null / "" = the appliance zone.
    timezone: Optional[str] = Field(None, max_length=64)

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_email(v)

    @field_validator("timezone")
    @classmethod
    def _v_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _clean_timezone(v)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=password_policy.MIN_LENGTH, max_length=password_policy.MAX_LENGTH)
    # The user must choose their own password after signing in with this one.
    must_change_password: bool = True


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=password_policy.MIN_LENGTH, max_length=password_policy.MAX_LENGTH)


class UserOut(BaseModel):
    id: UUID
    username: str
    email: str
    full_name: Optional[str]
    role: str
    is_active: bool
    auth_source: str = "local"
    scope_tags: list[str] = []
    # Scope inherited from the role (roles.scope_tags); filled by list/detail
    # endpoints. The effective scope is scope_tags plus these.
    role_scope_tags: list[str] = []
    last_login: Optional[datetime]
    timezone: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    must_change_password: bool = False
    mfa_enabled: bool = False
    locked_until: Optional[datetime] = None
    locked: bool = False
    password_changed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


def _user_out(user: User, role_scopes: Optional[dict[str, list[str]]] = None) -> UserOut:
    out = UserOut.model_validate(user)
    now = datetime.now(timezone.utc)
    out.locked = bool(user.locked_until and user.locked_until > now)
    if role_scopes is not None:
        out.role_scope_tags = list(role_scopes.get(user.role) or [])
    return out


async def _role_scopes(db: AsyncSession) -> dict[str, list[str]]:
    return {r.name: list(r.scope_tags or []) for r in await _db_roles(db)}


class RoleOut(BaseModel):
    id: str
    name: str
    description: str
    permissions: list[str]
    user_count: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _assert_unique_identity(db: AsyncSession, *, username: Optional[str], email: Optional[str],
                                  exclude_id: Optional[UUID] = None) -> None:
    """Field-level conflicts, case-insensitive, never revealing which account owns the value."""
    if username is not None:
        q = select(User.id).where(func.lower(User.username) == username.lower())
        if exclude_id:
            q = q.where(User.id != exclude_id)
        if (await db.execute(q.limit(1))).first():
            raise HTTPException(status_code=409, detail="That username is already taken")
    if email is not None:
        q = select(User.id).where(func.lower(User.email) == email.lower())
        if exclude_id:
            q = q.where(User.id != exclude_id)
        if (await db.execute(q.limit(1))).first():
            raise HTTPException(status_code=409, detail="That email address is already in use")


async def _commit_or_conflict(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        text_ = str(getattr(exc, "orig", exc)).lower()
        if "users_role_fk" in text_ or "foreign key" in text_:
            raise HTTPException(status_code=409, detail="The selected role no longer exists")
        raise HTTPException(status_code=409, detail="That username or email address is already in use")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.view", "users.manage", "roles.view", "roles.manage")),
):
    """List all available roles with descriptions and permissions.

    Kept for backward compatibility — the full role management API lives
    at /api/v1/roles.
    """
    result = await db.execute(
        select(User.role, func.count(User.id)).group_by(User.role)
    )
    counts = {row[0]: row[1] for row in result.all()}

    db_roles = await _db_roles(db)
    if db_roles:
        return [RoleOut(
            id=r.name,
            name=r.display_name,
            description=r.description or "",
            permissions=list(r.permissions or []),
            user_count=counts.get(r.name, 0),
        ) for r in db_roles]

    return [RoleOut(
        id=role_id,
        name=role_id.replace("_", " ").title(),
        description=desc,
        permissions=LEGACY_ROLE_PERMISSIONS.get(role_id, []),
        user_count=counts.get(role_id, 0),
    ) for role_id, desc in LEGACY_ROLE_DESCRIPTIONS.items()]


_SORTS = {
    "username": lambda: func.lower(User.username),
    "email": lambda: func.lower(User.email),
    "role": lambda: User.role,
    "last_login": lambda: User.last_login,
    "created": lambda: User.created_at,
    "status": lambda: User.is_active,
}


@router.get("", response_model=list[UserOut])
async def list_users(
    response: Response,
    q: Optional[str] = Query(None, max_length=100, description="Search username, email, full name"),
    role: Optional[str] = None,
    source: Optional[str] = Query(None, pattern="^(local|ldap|radius|oidc)$"),
    status: Optional[str] = Query(None, pattern="^(active|disabled|locked)$"),
    scoped: Optional[bool] = None,
    never_signed_in: Optional[bool] = None,
    mfa: Optional[bool] = None,
    sort: str = Query("username", pattern="^(username|email|role|last_login|created|status)$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.view", "users.manage")),
):
    """List users with optional filters, sorting and pagination. The total
    number of matches is returned in the ``X-Total-Count`` header."""
    conds = []
    if q:
        like = f"%{q.strip().lower()}%"
        conds.append(or_(func.lower(User.username).like(like), func.lower(User.email).like(like),
                         func.lower(func.coalesce(User.full_name, "")).like(like)))
    if role:
        conds.append(User.role == role)
    if source:
        conds.append(User.auth_source == source)
    now = datetime.now(timezone.utc)
    if status == "active":
        conds.append(and_(User.is_active.is_(True), or_(User.locked_until.is_(None), User.locked_until <= now)))
    elif status == "disabled":
        conds.append(User.is_active.is_(False))
    elif status == "locked":
        conds.append(and_(User.locked_until.is_not(None), User.locked_until > now))
    # Scoped by their own tags or by their role's (roles.scope_tags).
    role_scoped = User.role.in_(select(Role.name).where(func.jsonb_array_length(Role.scope_tags) > 0))
    if scoped is True:
        conds.append(or_(func.jsonb_array_length(User.scope_tags) > 0, role_scoped))
    elif scoped is False:
        conds.append(and_(func.jsonb_array_length(User.scope_tags) == 0, ~role_scoped))
    if never_signed_in is True:
        conds.append(User.last_login.is_(None))
    elif never_signed_in is False:
        conds.append(User.last_login.is_not(None))
    if mfa is not None:
        conds.append(User.mfa_enabled.is_(mfa))

    total = (await db.execute(select(func.count(User.id)).where(*conds))).scalar() or 0
    sort_col = _SORTS[sort]()
    ordering = sort_col.desc().nulls_last() if order == "desc" else sort_col.asc().nulls_last()
    rows = (await db.execute(
        select(User).where(*conds).order_by(ordering, User.username).limit(limit).offset(offset)
    )).scalars().all()
    response.headers["X-Total-Count"] = str(total)
    role_scopes = await _role_scopes(db)
    return [_user_out(u, role_scopes) for u in rows]


@router.get("/access-review")
async def access_review_export(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.view", "users.manage")),
):
    """CSV for periodic access recertification (CIS Control 6.8): every account
    with its role, effective high-risk permissions, scope, source, MFA state and
    last sign-in."""
    roles = {r.name: r for r in await _db_roles(db)}
    users = (await db.execute(select(User).order_by(func.lower(User.username)))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["username", "full_name", "email", "status", "locked", "auth_source", "role",
                "permission_count", "privileged_permissions", "visibility_scope", "mfa_enabled",
                "must_change_password", "last_sign_in_utc", "created_utc"])
    now = datetime.now(timezone.utc)
    for u in users:
        role = roles.get(u.role)
        perms = effective_permissions(role.permissions if role else LEGACY_ROLE_PERMISSIONS.get(u.role, []))
        risky = sorted(p for p in perms if risk_of(p) in ("privileged", "critical"))
        own_and_role = list(u.scope_tags or []) + [t for t in (role.scope_tags if role else None) or []
                                                    if t.lower() not in {x.lower() for x in (u.scope_tags or [])}]
        scope = ", ".join(own_and_role) if (own_and_role and SUPERUSER_PERMISSION not in perms) else (
            "all resources (full administration)" if SUPERUSER_PERMISSION in perms else "all resources")

        def cell(v: str) -> str:   # neutralize spreadsheet formula injection
            return ("'" + v) if v and v[0] in "=+-@\t\r" else v
        w.writerow([cell(u.username), cell(u.full_name or ""), cell(u.email),
                    "active" if u.is_active else "disabled",
                    "yes" if (u.locked_until and u.locked_until > now) else "no", u.auth_source, u.role,
                    len(perms), " ".join(risky), cell(scope), "yes" if u.mfa_enabled else "no",
                    "yes" if u.must_change_password else "no",
                    u.last_login.isoformat() if u.last_login else "", u.created_at.isoformat() if u.created_at else ""])
    await write_audit_log(db, actor=current_user, action="user.access_review_export", resource_type="user",
                          metadata={"accounts": len(users)})
    await db.commit()
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="zenplus-access-review-{now:%Y%m%d}.csv"'},
    )


@router.post("/me/change-password", status_code=200)
async def change_own_password(
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change your own password (local accounts only)."""
    from app.core.security import verify_password
    if getattr(current_user, "auth_source", "local") != "local":
        raise HTTPException(
            status_code=400,
            detail=f"Your account authenticates via {current_user.auth_source.upper()}; change your password there",
        )
    if not verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if data.new_password == data.current_password:
        raise HTTPException(status_code=422, detail="Choose a password different from your current one")
    problems = password_policy.problems(data.new_password, username=current_user.username,
                                        email=current_user.email, full_name=current_user.full_name)
    if problems:
        raise HTTPException(status_code=422, detail=problems[0])

    current_user.password_hash = hash_password(data.new_password)
    current_user.must_change_password = False
    current_user.password_changed_at = datetime.now(timezone.utc)
    current_user.updated_at = datetime.now(timezone.utc)
    await session_service.revoke_all_sessions(db, current_user.id, by=current_user.username,
                                              reason="password changed")
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
                "All existing sessions have been signed out. Sign in again with your new password.",
        changed_by="you",
    )
    return {"message": "Password changed successfully"}


# ── Contact methods (escalation targets, migrate-164) ───────────────────────
#
# How an on-call responder is reached: email and SMS addresses. The account
# email is the implicit email contact while a user has no email method of
# their own. A user edits their own (``/users/me/contact-methods``);
# alerts.manage or users.manage edit anyone's.

MAX_CONTACT_METHODS = 10


class ContactMethodIn(BaseModel):
    kind: str = Field(..., pattern="^(email|sms)$")
    address: str = Field(..., min_length=3, max_length=255)
    label: Optional[str] = Field(None, max_length=100)
    enabled: bool = True


class ContactMethodsBody(BaseModel):
    methods: list[ContactMethodIn] = Field(default_factory=list, max_length=MAX_CONTACT_METHODS)
    # Present = set (null / "" = the appliance zone); absent = unchanged.
    timezone: Optional[str] = Field(None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _v_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _clean_timezone(v)


async def _contact_payload(db: AsyncSession, user_id) -> dict:
    row = (await db.execute(text("SELECT id, email, timezone FROM users WHERE id = :id"),
                            {"id": user_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    methods = [{"id": str(m.id), "kind": m.kind, "address": m.address, "label": m.label,
                "enabled": bool(m.enabled)}
               for m in (await db.execute(text(
                   "SELECT id, kind, address, label, enabled FROM user_contact_methods"
                   " WHERE user_id = :id ORDER BY created_at, id"), {"id": user_id})).fetchall()]
    return {"user_id": str(row.id), "timezone": row.timezone, "account_email": row.email,
            "methods": methods,
            # The account email is used while no enabled email method exists.
            "implicit_email": not any(m["kind"] == "email" and m["enabled"] for m in methods)}


async def _save_contacts(db: AsyncSession, actor: User, user_id, body: ContactMethodsBody) -> dict:
    from app.services.oncall_service import normalize_address, valid_address

    clean, seen = [], set()
    for m in body.methods:
        addr = m.address.strip()
        if not valid_address(m.kind, addr):
            raise HTTPException(status_code=422, detail=(
                f"{addr!r} is not a valid email address" if m.kind == "email"
                else f"{addr!r} is not a valid phone number (digits, optional leading +)"))
        key = (m.kind, normalize_address(m.kind, addr))
        if key in seen:
            raise HTTPException(status_code=422, detail=f"{addr} is listed twice")
        seen.add(key)
        clean.append({"kind": m.kind, "address": addr, "label": (m.label or "").strip() or None,
                      "enabled": m.enabled})
    exists = (await db.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": user_id})).first()
    if not exists:
        raise HTTPException(status_code=404, detail="User not found")
    before = await _contact_payload(db, user_id)
    await db.execute(text("DELETE FROM user_contact_methods WHERE user_id = :id"), {"id": user_id})
    for m in clean:
        await db.execute(text(
            "INSERT INTO user_contact_methods (user_id, kind, address, label, enabled)"
            " VALUES (:uid, :kind, :address, :label, :enabled)"), {"uid": user_id, **m})
    changes = {"methods": {"from": len(before["methods"]), "to": len(clean)}}
    if "timezone" in body.model_fields_set:
        await db.execute(text("UPDATE users SET timezone = :tz, updated_at = NOW() WHERE id = :id"),
                         {"tz": body.timezone, "id": user_id})
        changes["timezone"] = {"from": before["timezone"], "to": body.timezone}
    await write_audit_log(db, actor=actor, action="user.contact_methods.update", resource_type="user",
                          resource_id=str(user_id),
                          metadata={"changes": changes,
                                    "kinds": sorted({m["kind"] for m in clean})})
    await db.commit()
    return await _contact_payload(db, user_id)


@router.get("/me/contact-methods")
async def my_contact_methods(current_user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    return await _contact_payload(db, current_user.id)


@router.put("/me/contact-methods")
async def update_my_contact_methods(body: ContactMethodsBody,
                                    current_user: User = Depends(get_current_user),
                                    db: AsyncSession = Depends(get_db)):
    return await _save_contacts(db, current_user, current_user.id, body)


@router.get("/{user_id}/contact-methods")
async def user_contact_methods(user_id: UUID, db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(require_permission("alerts.manage", "users.manage"))):
    return await _contact_payload(db, user_id)


@router.put("/{user_id}/contact-methods")
async def update_user_contact_methods(user_id: UUID, body: ContactMethodsBody,
                                      db: AsyncSession = Depends(get_db),
                                      current_user: User = Depends(
                                          require_permission("alerts.manage", "users.manage"))):
    return await _save_contacts(db, current_user, user_id, body)


@router.get("/me/sessions")
async def my_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Where you are signed in. (The current session is flagged by the client
    matching its own session id from /auth/me.)"""
    return {"data": await session_service.list_sessions(db, current_user.id)}


@router.delete("/me/sessions/{session_id}", status_code=200)
async def revoke_my_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not await session_service.revoke_session(db, str(session_id), current_user.id,
                                                by=current_user.username, reason="revoked by owner"):
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return {"message": "Session revoked"}


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Create a new local user. The actor can only assign a role whose access
    is within their own (a delegated user manager cannot create administrators)."""
    role = await _role_by_name(db, data.role)
    valid_roles = await _valid_role_names(db)
    if data.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")
    if role is not None:
        await delegation.assert_can_assign_role(db, current_user, role)

    problems = password_policy.problems(data.password, username=data.username, email=data.email,
                                        full_name=data.full_name)
    if problems:
        raise HTTPException(status_code=422, detail=problems[0])

    await _assert_unique_identity(db, username=data.username, email=data.email)
    role_scope = list(role.scope_tags or []) if role is not None else []
    scope_tags = await delegation.validate_assigned_scope(
        db, current_user, await tag_service.canonicalize_tags(db, data.scope_tags), role_scope=role_scope)
    await delegation.assert_effective_scope_within(db, current_user, scope_tags, role_scope)

    user = User(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        role=data.role,
        is_active=data.is_active,
        auth_source="local",
        scope_tags=scope_tags,
        must_change_password=data.must_change_password,
        password_changed_at=datetime.now(timezone.utc),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="That username or email address is already in use")
    await write_audit_log(
        db,
        actor=current_user,
        action="user.create",
        resource_type="user",
        resource_id=str(user.id),
        metadata={"username": user.username, "role": user.role, "is_active": user.is_active,
                  "scope_tags": user.scope_tags, "must_change_password": user.must_change_password},
    )
    await _commit_or_conflict(db)
    await db.refresh(user)
    return _user_out(user)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.view", "users.manage")),
):
    return _user_out(await _get_user_or_404(db, user_id), await _role_scopes(db))


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Update user details (patch semantics). Sessions are revoked only when the
    role, enabled state or visibility scope actually changes."""
    fields = data.model_fields_set
    user = await _get_user_or_404(db, user_id)
    is_self = user.id == current_user.id

    # Serialize every administrator-affecting change before reading state.
    await admin_guard.lock_admin_invariant(db)
    admins_before = await admin_guard.count_active_full_admins(db)

    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))

    changes: dict = {}
    access_changed = False

    if "role" in fields and data.role is not None and data.role != user.role:
        if user.auth_source != "local":
            raise HTTPException(
                status_code=409,
                detail=f"This account's role is set by {user.auth_source.upper()} role mapping on every sign-in; "
                       "change the mapping instead",
            )
        if is_self:
            raise HTTPException(status_code=400, detail="You cannot change your own role")
        valid_roles = await _valid_role_names(db)
        if data.role not in valid_roles:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")
        new_role = await _role_by_name(db, data.role)
        if new_role is not None:
            await delegation.assert_can_assign_role(db, current_user, new_role)
        changes["role"] = {"from": user.role, "to": data.role}
        user.role = data.role
        access_changed = True

    if "email" in fields and data.email is not None and data.email != user.email:
        await _assert_unique_identity(db, username=None, email=data.email, exclude_id=user.id)
        changes["email"] = {"from": user.email, "to": data.email}
        user.email = data.email

    if "full_name" in fields:
        new_name = (data.full_name or "").strip() or None
        if new_name != user.full_name:
            changes["full_name"] = {"from": user.full_name, "to": new_name}
            user.full_name = new_name

    if "timezone" in fields and data.timezone != user.timezone:
        changes["timezone"] = {"from": user.timezone, "to": data.timezone}
        user.timezone = data.timezone

    if "scope_tags" in fields and data.scope_tags is not None:
        canonical = await tag_service.canonicalize_tags(db, data.scope_tags)
        if {t.lower() for t in canonical} != {t.lower() for t in (user.scope_tags or [])}:
            if is_self and not await delegation.is_full_admin(db, current_user):
                raise HTTPException(status_code=400, detail="You cannot change your own visibility scope")
            final_role = await _role_by_name(db, user.role)
            canonical = await delegation.validate_assigned_scope(
                db, current_user, canonical,
                role_scope=list(final_role.scope_tags or []) if final_role is not None else [])
            changes["scope_tags"] = {"from": list(user.scope_tags or []), "to": canonical}
            user.scope_tags = canonical
            access_changed = True

    if "role" in changes or "scope_tags" in changes:
        # Own tags plus the (possibly new) role's tags must stay within a
        # scoped actor's scope: moving a user to an unscoped role must not
        # leave them unrestricted.
        final_role = await _role_by_name(db, user.role)
        await delegation.assert_effective_scope_within(
            db, current_user, user.scope_tags,
            list(final_role.scope_tags or []) if final_role is not None else [])

    if "is_active" in fields and data.is_active is not None and data.is_active != user.is_active:
        if is_self and not data.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
        changes["is_active"] = {"from": user.is_active, "to": data.is_active}
        user.is_active = data.is_active
        access_changed = True

    if "must_change_password" in fields and data.must_change_password is not None \
            and data.must_change_password != user.must_change_password:
        if user.auth_source != "local":
            raise HTTPException(status_code=400, detail="Password changes are managed by the directory for this account")
        changes["must_change_password"] = {"from": user.must_change_password, "to": data.must_change_password}
        user.must_change_password = data.must_change_password

    if not changes:
        return _user_out(user)

    await db.flush()
    await admin_guard.assert_admin_survives(db, admins_before)

    user.updated_at = datetime.now(timezone.utc)
    if access_changed:
        revoked = await session_service.revoke_all_sessions(
            db, user.id, by=current_user.username, reason="access changed")
        changes["sessions_revoked"] = revoked
    await write_audit_log(db, actor=current_user, action="user.update", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username, "changes": changes})
    await _commit_or_conflict(db)
    await db.refresh(user)
    return _user_out(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Delete a user. Cannot delete yourself, an account above your own access,
    or the last active administrator."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    await admin_guard.lock_admin_invariant(db)
    admins_before = await admin_guard.count_active_full_admins(db)
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))

    deleted_username, deleted_role = user.username, user.role
    await db.delete(user)
    await db.flush()
    await admin_guard.assert_admin_survives(db, admins_before)
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
    current_user: User = Depends(require_permission("users.manage")),
):
    """Reset a local user's password. Revokes every session of that account; the
    actor must be within the delegation boundary of the target."""
    user = await _get_user_or_404(db, user_id)
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Use 'Change password' for your own account")
    if getattr(user, "auth_source", "local") != "local":
        raise HTTPException(
            status_code=400,
            detail=f"This account authenticates via {user.auth_source.upper()}; its password is managed externally",
        )
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    problems = password_policy.problems(data.new_password, username=user.username, email=user.email,
                                        full_name=user.full_name)
    if problems:
        raise HTTPException(status_code=422, detail=problems[0])

    user.password_hash = hash_password(data.new_password)
    user.must_change_password = data.must_change_password
    user.password_changed_at = datetime.now(timezone.utc)
    user.failed_login_count = 0
    user.locked_until = None
    user.updated_at = datetime.now(timezone.utc)
    revoked = await session_service.revoke_all_sessions(
        db, user.id, by=current_user.username, reason="password reset")
    await write_audit_log(
        db,
        actor=current_user,
        action="user.password_reset",
        resource_type="user",
        resource_id=str(user.id),
        metadata={"username": user.username, "must_change_password": data.must_change_password,
                  "sessions_revoked": revoked},
    )
    await db.commit()
    await _send_account_notice(
        db, user,
        subject="[ZenPlus] Your password was reset",
        title="Your password was reset",
        message="An administrator has reset the password for your ZenPlus "
                "account and signed you out everywhere. Use the new password "
                "provided to you to sign in" + (" and choose your own when prompted." if data.must_change_password else "."),
        changed_by=f"{current_user.username} (administrator)",
    )
    return {"message": "Password reset. The account was signed out of all sessions.",
            "sessions_revoked": revoked, "must_change_password": data.must_change_password}


@router.post("/{user_id}/unlock", status_code=200)
async def unlock_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Clear a failed-sign-in lockout."""
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    user.failed_login_count = 0
    user.locked_until = None
    await write_audit_log(db, actor=current_user, action="user.unlock", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username})
    await db.commit()
    return {"message": "Account unlocked"}


@router.get("/{user_id}/sessions")
async def list_user_sessions(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    return {"data": await session_service.list_sessions(db, user.id)}


@router.post("/{user_id}/revoke-sessions", status_code=200)
async def revoke_user_sessions(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Sign an account out everywhere."""
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    revoked = await session_service.revoke_all_sessions(
        db, user.id, by=current_user.username, reason="revoked by administrator")
    await write_audit_log(db, actor=current_user, action="user.sessions_revoked", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username, "sessions": revoked})
    await db.commit()
    return {"message": "All sessions revoked", "sessions_revoked": revoked}


@router.delete("/{user_id}/sessions/{session_id}", status_code=200)
async def revoke_user_session(
    user_id: UUID,
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    if not await session_service.revoke_session(db, str(session_id), user.id, by=current_user.username,
                                                reason="revoked by administrator"):
        raise HTTPException(status_code=404, detail="Session not found")
    await write_audit_log(db, actor=current_user, action="user.session_revoked", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username, "session": str(session_id)})
    await db.commit()
    return {"message": "Session revoked"}


@router.post("/{user_id}/mfa/reset", status_code=200)
async def reset_user_mfa(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Remove a user's multi-factor enrolment (lost device). They re-enrol at
    next sign-in when policy requires it. Revokes all their sessions."""
    user = await _get_user_or_404(db, user_id)
    await delegation.assert_can_manage_user(db, current_user, user, await _role_permissions(db, user.role))
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_recovery_hashes = []
    user.mfa_enrolled_at = None
    user.mfa_last_counter = None
    await session_service.revoke_all_sessions(db, user.id, by=current_user.username, reason="MFA reset")
    await write_audit_log(db, actor=current_user, action="user.mfa_reset", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username})
    await db.commit()
    return {"message": "Multi-factor authentication removed"}


@router.get("/{user_id}/effective-access")
async def effective_access(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users.view", "users.manage")),
):
    """What this account can actually do and see, in one place: role, the
    permissions that role grants (grouped, with risk), visibility scope and its
    bypass, sign-in source/MFA, and the latest audit events about the account."""
    user = await _get_user_or_404(db, user_id)
    role = await _role_by_name(db, user.role)
    granted = list(role.permissions or []) if role else list(LEGACY_ROLE_PERMISSIONS.get(user.role, []))
    full = SUPERUSER_PERMISSION in granted
    eff = effective_permissions(granted)
    modules = []
    for mid, label, perms in PERMISSION_MODULES:
        items = [{"id": pid, "label": pl, "granted": pid in eff, "explicit": pid in granted,
                  "risk": risk_of(pid)} for pid, pl, _ in perms]
        modules.append({"module": mid, "label": label, "permissions": items,
                        "granted_count": sum(1 for i in items if i["granted"]), "total": len(items)})
    own_scope = list(user.scope_tags or [])
    role_scope = list(role.scope_tags or []) if role else []
    scope = own_scope + [t for t in role_scope if t.lower() not in {x.lower() for x in own_scope}]
    scope_state = "all" if (full or not scope) else "scoped"
    history = (await db.execute(text(
        "SELECT created_at, action, actor_username, metadata FROM audit_logs "
        "WHERE resource_type = 'user' AND resource_id = :rid ORDER BY created_at DESC LIMIT 15"),
        {"rid": str(user.id)})).all()
    session_count = len(await session_service.list_sessions(db, user.id))
    return {
        "user": _user_out(user, {user.role: role_scope}).model_dump(mode="json"),
        "role": {"name": user.role, "display_name": role.display_name if role else user.role,
                 "exists": role is not None, "full_administration": full},
        "permissions": modules,
        "permission_count": len(eff),
        "privileged_permissions": sorted(p for p in eff if risk_of(p) in ("privileged", "critical")),
        "scope": {
            "state": scope_state,
            "tags": scope,
            "user_tags": own_scope,
            "role_tags": role_scope,
            "ignored_because_full_administration": bool(full and scope),
            "note": ("Full administration bypasses visibility scope; stored tags apply again if the role changes."
                     if full else
                     "Sees only resources carrying at least one of these tags; untagged resources are hidden."
                     if scope else "Sees every resource the role permits."),
        },
        "sessions": session_count,
        "history": [{"at": h[0], "action": h[1], "actor": h[2], "details": h[3]} for h in history],
    }
