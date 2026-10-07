import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from jwt.exceptions import InvalidTokenError as JWTError
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import get_db
from app.core.permissions import (
    LEGACY_ROLE_PERMISSIONS,
    SUPERUSER_PERMISSION,
    has_permission,
)
from app.models.user import User

settings = get_settings()
logger = logging.getLogger(__name__)
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

# ─────────────────────────────────────────────────────────────────
# Role → permission resolution. Roles live in the `roles` table
# (migrate-074). Authorization is decided ONLY by the permissions a
# role row grants — never by the role's name. The hardcoded legacy
# vocabulary is used solely when the roles table itself does not exist
# yet (an appliance mid-update, before the migration lands).
#
# A short-lived per-process cache keeps the per-request cost at zero.
# Role edits bump a shared revision (roles.revision) and the cache TTL
# is the documented upper bound for another worker to notice a change:
# ROLE_POLICY_MAX_STALENESS_SECONDS.
# ─────────────────────────────────────────────────────────────────

ROLE_POLICY_MAX_STALENESS_SECONDS = 5.0

_ROLE_CACHE: dict[str, tuple[float, list[str]]] = {}
_ROLE_SCOPE_CACHE: dict[str, tuple[float, list[str]]] = {}

# Scope used when a role's scope cannot be read and nothing was cached: longer
# than any tag may be (tag_service.MAX_TAG_LEN = 64), so no entity can carry
# it and a lookup failure hides resources instead of revealing them.
SCOPE_LOOKUP_FAILED = "zenplus:role-scope-unavailable:" + "0" * 48


def invalidate_role_cache() -> None:
    _ROLE_CACHE.clear()
    _ROLE_SCOPE_CACHE.clear()


def _is_missing_relation(exc: BaseException) -> bool:
    """True when the failure means the roles table does not exist yet."""
    orig = getattr(exc, "orig", exc)
    name = type(orig).__name__
    if name == "UndefinedTableError":
        return True
    msg = str(orig).lower()
    return "relation" in msg and "does not exist" in msg and "roles" in msg


async def get_role_permissions(db: AsyncSession, role_name: str) -> list[str]:
    """Permissions granted by ``role_name``.

    Fail-closed: a lookup error never widens access. The last successfully
    resolved policy is reused (even when stale) and, with none, the role is
    treated as having no permissions. The lookup runs in a savepoint so a
    failure cannot poison or roll back the caller's transaction.
    """
    now = time.monotonic()
    hit = _ROLE_CACHE.get(role_name)
    if hit and now - hit[0] < ROLE_POLICY_MAX_STALENESS_SECONDS:
        return hit[1]

    try:
        async with db.begin_nested():
            row = (await db.execute(
                text("SELECT permissions FROM roles WHERE name = :name"),
                {"name": role_name},
            )).first()
    except Exception as exc:
        if _is_missing_relation(exc):
            perms = list(LEGACY_ROLE_PERMISSIONS.get(role_name, []))
            _ROLE_CACHE[role_name] = (now, perms)
            return perms
        logger.error("role permission lookup failed for %r; keeping last valid policy",
                     role_name, exc_info=True)
        return hit[1] if hit else []

    if row is not None and isinstance(row[0], list):
        perms = [p for p in row[0] if isinstance(p, str)]
    else:
        # The table exists but has no such role (deleted / never created):
        # that account has no authority, whatever its name suggests.
        perms = []

    _ROLE_CACHE[role_name] = (now, perms)
    return perms


def _is_missing_scope_column(exc: BaseException) -> bool:
    """True when roles.scope_tags does not exist yet (before migrate-154)."""
    orig = getattr(exc, "orig", exc)
    if type(orig).__name__ == "UndefinedColumnError":
        return True
    msg = str(orig).lower()
    return "scope_tags" in msg and "does not exist" in msg


async def get_role_scope_tags(db: AsyncSession, role_name: str) -> list[str]:
    """Visibility scope tags every member of ``role_name`` is limited to
    (``roles.scope_tags``, migrate-154). Empty = the role adds no limit.

    Same caching and staleness bound as :func:`get_role_permissions`. Fail-closed:
    on a lookup error the last resolved scope is reused, and with none the
    role is treated as scoped to a tag that matches nothing. A database without
    the column (or without the roles table) has no role scopes at all.
    """
    now = time.monotonic()
    hit = _ROLE_SCOPE_CACHE.get(role_name)
    if hit and now - hit[0] < ROLE_POLICY_MAX_STALENESS_SECONDS:
        return hit[1]

    try:
        async with db.begin_nested():
            row = (await db.execute(
                text("SELECT scope_tags FROM roles WHERE name = :name"),
                {"name": role_name},
            )).first()
    except Exception as exc:
        if _is_missing_relation(exc) or _is_missing_scope_column(exc):
            _ROLE_SCOPE_CACHE[role_name] = (now, [])
            return []
        logger.error("role scope lookup failed for %r; keeping last valid scope",
                     role_name, exc_info=True)
        return hit[1] if hit else [SCOPE_LOOKUP_FAILED]

    raw = row[0] if row is not None else None
    tags = [str(t) for t in raw if isinstance(t, str) and t.strip()] if isinstance(raw, list) else []
    _ROLE_SCOPE_CACHE[role_name] = (now, tags)
    return tags


async def user_has_permission(db: AsyncSession, user: User, permission: str) -> bool:
    perms = await get_role_permissions(db, user.role)
    return has_permission(perms, permission)


async def user_is_full_admin(db: AsyncSession, user: User) -> bool:
    return SUPERUSER_PERMISSION in await get_role_permissions(db, user.role)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except UnknownHashError:
        return False


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# Requests a signed-in account may still make while it owes a password change
# or MFA enrolment. Everything else is refused until the obligation is met.
_OBLIGATION_ALLOWED_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/users/me/",
    "/api/v1/system/time",
)


STREAM_TICKET_SECONDS = 60


def create_stream_ticket(user: User, jti: Optional[str]) -> str:
    """Short-lived, single-purpose token for browser EventSource connections
    (which cannot send an Authorization header). It is only accepted by the
    streaming endpoints, expires in a minute, and dies with the session."""
    return create_access_token(
        {"sub": str(user.id), "purpose": "stream", "jti": jti,
         "ver": getattr(user, "token_version", 0) or 0},
        expires_delta=timedelta(seconds=STREAM_TICKET_SECONDS),
    )


async def _authenticate_token(
    raw_token: str,
    request: Optional[Request],
    db: AsyncSession,
    *,
    allow_purpose: Optional[str] = None,
) -> User:
    """Resolve a bearer token to an active user, enforcing token version,
    server-side session revocation and sign-in obligations."""
    try:
        payload = jwt.decode(raw_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if allow_purpose is not None and payload.get("purpose") != allow_purpose:
        # An endpoint that takes a purpose token takes ONLY that token.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if payload.get("purpose") and payload.get("purpose") != allow_purpose:
        # Short-lived purpose tokens (MFA challenge, stream ticket) are not
        # session tokens and only work where explicitly allowed.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    if payload.get("ver", 0) != (getattr(user, "token_version", 0) or 0):
        raise HTTPException(401, "Session revoked; sign in again")

    jti = payload.get("jti")
    if jti:
        from app.services import session_service
        await session_service.assert_session_active(db, str(jti), user.id)

    if request is not None:
        await _enforce_sign_in_obligations(db, user, request)
    return user


async def _enforce_sign_in_obligations(db: AsyncSession, user: User, request: Request) -> None:
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _OBLIGATION_ALLOWED_PREFIXES):
        return
    if getattr(user, "must_change_password", False):
        raise HTTPException(status_code=403, detail={
            "code": "password_change_required",
            "message": "You must change your password before continuing",
        })
    if not getattr(user, "mfa_enabled", False) and getattr(user, "auth_source", "local") == "local":
        from app.services import auth_policy
        if await auth_policy.mfa_required_for(db, user):
            raise HTTPException(status_code=403, detail={
                "code": "mfa_enrollment_required",
                "message": "Multi-factor authentication must be set up for this account",
            })


async def get_current_user(
    request: Request = None,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    if request:
        from app.services.management_access import check_web_access
        check_web_access(request.client.host if request.client else "unknown")
    return await _authenticate_token(credentials.credentials, request, db)


# ─────────────────────────────────────────────────────────────────
# Route guards. Every guard carries ``_zp_guard`` so the route-policy
# layer (app.core.route_policy) knows a route already declares its own
# authorization and the module default must not second-guess it.
# ─────────────────────────────────────────────────────────────────

def _mark_guard(fn, kind: str, permissions: tuple[str, ...] = ()):
    fn._zp_guard = kind  # type: ignore[attr-defined]
    fn._zp_permissions = tuple(permissions)  # type: ignore[attr-defined]
    return fn


def require_permission(*permissions: str):
    """Pass when the user's role grants any of the given permissions
    (``system.admin`` always passes)."""

    async def dependency(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        perms = await get_role_permissions(db, user.role)
        if SUPERUSER_PERMISSION in perms or any(p in perms for p in permissions):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    return _mark_guard(dependency, "permission", permissions)


async def require_admin_user(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    perms = await get_role_permissions(db, user.role)
    if SUPERUSER_PERMISSION in perms:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required",
    )


_mark_guard(require_admin_user, "admin", (SUPERUSER_PERMISSION,))


# ─────────────────────────────────────────────────────────────────
# EventSource (SSE) friendly auth — browser EventSource can't send
# Authorization headers, so streaming endpoints accept the token as
# the `?token=` query param as a fallback. Same JWT path otherwise.
# ─────────────────────────────────────────────────────────────────

async def get_current_user_stream(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    token: Optional[str] = Query(default=None),
    ticket: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate a streaming request: an Authorization header, a one-minute
    ``?ticket=`` (what the browser uses), or - for older clients - ``?token=``."""
    if request:
        from app.services.management_access import check_web_access
        check_web_access(request.client.host if request.client else "unknown")
    if ticket and not credentials:
        return await _authenticate_token(ticket, request, db, allow_purpose="stream")
    raw = credentials.credentials if credentials else token
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    return await _authenticate_token(raw, request, db)


def require_permission_stream(*permissions: str):
    """SSE variant of :func:`require_permission` (token may arrive as ?token=)."""

    async def dependency(
        user: User = Depends(get_current_user_stream),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        perms = await get_role_permissions(db, user.role)
        if SUPERUSER_PERMISSION in perms or any(p in perms for p in permissions):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    return _mark_guard(dependency, "permission", permissions)
