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
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

# Legacy shortcuts, kept as a safety net for appliances whose roles table
# has not landed yet (mid-update) and for test fixtures with fake users.
ADMIN_ROLES = {"admin", "owner"}
OPERATOR_ROLES = {"admin", "owner", "operator"}

# ─────────────────────────────────────────────────────────────────
# Role → permission resolution. Roles live in the `roles` table
# (migrate-074); a short-lived cache keeps the per-request cost at
# zero without letting role edits linger for more than a few seconds.
# ─────────────────────────────────────────────────────────────────

_ROLE_CACHE: dict[str, tuple[float, list[str]]] = {}
_ROLE_CACHE_TTL = 15.0


def invalidate_role_cache() -> None:
    _ROLE_CACHE.clear()


async def get_role_permissions(db: AsyncSession, role_name: str) -> list[str]:
    now = time.monotonic()
    hit = _ROLE_CACHE.get(role_name)
    if hit and now - hit[0] < _ROLE_CACHE_TTL:
        return hit[1]

    perms: Optional[list[str]] = None
    try:
        row = (await db.execute(
            text("SELECT permissions FROM roles WHERE name = :name"),
            {"name": role_name},
        )).first()
        if row is not None and isinstance(row[0], list):
            perms = [p for p in row[0] if isinstance(p, str)]
    except Exception:
        # Roles table missing (pre-migration) — recover the session and
        # fall back to the hardcoded legacy vocabulary.
        try:
            await db.rollback()
        except Exception:
            pass

    if perms is None:
        perms = LEGACY_ROLE_PERMISSIONS.get(role_name, [])

    _ROLE_CACHE[role_name] = (now, perms)
    return perms


async def user_has_permission(db: AsyncSession, user: User, permission: str) -> bool:
    perms = await get_role_permissions(db, user.role)
    return has_permission(perms, permission)


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
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    if payload.get("ver", 0) != (getattr(user, "token_version", 0) or 0):
        raise HTTPException(401, "Session revoked; sign in again")
    return user


def require_roles(*roles: str):
    allowed = set(roles)

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return dependency


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

    return dependency


async def require_admin_user(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if user.role in ADMIN_ROLES:
        return user
    perms = await get_role_permissions(db, user.role)
    if SUPERUSER_PERMISSION in perms:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required",
    )


async def require_operator_user(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if user.role in OPERATOR_ROLES:
        return user
    perms = await get_role_permissions(db, user.role)
    if SUPERUSER_PERMISSION in perms or "devices.manage" in perms:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Operator access required",
    )


# ─────────────────────────────────────────────────────────────────
# EventSource (SSE) friendly auth — browser EventSource can't send
# Authorization headers, so streaming endpoints accept the token as
# the `?token=` query param as a fallback. Same JWT path otherwise.
# ─────────────────────────────────────────────────────────────────

async def get_current_user_stream(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if request:
        from app.services.management_access import check_web_access
        check_web_access(request.client.host if request.client else "unknown")
    raw = credentials.credentials if credentials else token
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(raw, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if payload.get("ver", 0) != (getattr(user, "token_version", 0) or 0):
        raise HTTPException(401, "Session revoked; sign in again")
    return user


async def require_operator_user_stream(
    user: User = Depends(get_current_user_stream),
    db: AsyncSession = Depends(get_db),
) -> User:
    if user.role in OPERATOR_ROLES:
        return user
    perms = await get_role_permissions(db, user.role)
    if SUPERUSER_PERMISSION in perms or "devices.manage" in perms:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Operator access required",
    )
