import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    get_current_user,
    get_role_permissions,
    verify_password,
)
from app.core.config import get_settings
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse, UserResponse
from app.services.external_auth import (
    EXTERNAL_PASSWORD_HASH,
    LDAP_DEFAULTS,
    LDAP_SETTINGS_KEY,
    RADIUS_DEFAULTS,
    RADIUS_SETTINGS_KEY,
    ExternalAuthError,
    ldap_authenticate,
    map_ldap_role,
    map_radius_role,
    merge_config,
    radius_authenticate,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()
logger = logging.getLogger(__name__)


async def _user_response(db: AsyncSession, user: User) -> UserResponse:
    resp = UserResponse.model_validate(user)
    resp.permissions = await get_role_permissions(db, user.role)
    return resp


async def _email_available(db: AsyncSession, email: str, own_id) -> bool:
    result = await db.execute(select(User.id).where(User.email == email, User.id != own_id))
    return result.first() is None


async def _external_login(
    db: AsyncSession, username: str, password: str, existing: Optional[User]
) -> Optional[User]:
    """Authenticate against LDAP/RADIUS and provision or refresh the
    matching local user row. Returns None on bad credentials; raises 403
    for policy denials (provider disabled, no role mapped, provisioning
    off)."""
    from app.api.v1.settings import _get_system_setting

    ldap_cfg = merge_config(LDAP_DEFAULTS, await _get_system_setting(db, LDAP_SETTINGS_KEY))
    radius_cfg = merge_config(RADIUS_DEFAULTS, await _get_system_setting(db, RADIUS_SETTINGS_KEY))

    if existing is not None:
        # Known external account: only its own provider may verify it.
        cfg = ldap_cfg if existing.auth_source == "ldap" else radius_cfg
        if not cfg["enabled"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{existing.auth_source.upper()} authentication is disabled",
            )
        sources = [(existing.auth_source, cfg)]
    else:
        sources = [(s, c) for s, c in (("ldap", ldap_cfg), ("radius", radius_cfg)) if c["enabled"]]

    for source, cfg in sources:
        email: Optional[str] = None
        full_name: Optional[str] = None
        try:
            if source == "ldap":
                info = await asyncio.to_thread(ldap_authenticate, cfg, username, password)
                if info is None:
                    continue
                role = map_ldap_role(cfg, info["groups"])
                email, full_name = info["email"], info["full_name"]
            else:
                info = await asyncio.to_thread(radius_authenticate, cfg, username, password)
                if info is None:
                    continue
                role = map_radius_role(cfg, info["reply_values"])
        except ExternalAuthError:
            logger.warning("%s authentication error for %r", source, username, exc_info=True)
            continue

        if role is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{source.upper()} sign-in succeeded, but no ZenPlus role is mapped for this account",
            )

        if existing is None:
            if not cfg.get("auto_provision"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Automatic account creation is disabled for {source.upper()} users",
                )
            candidate = email or f"{username}@{source}.external"
            if not await _email_available(db, candidate, own_id=None):
                candidate = f"{username}@{source}.external"
            user = User(
                username=username,
                email=candidate,
                password_hash=EXTERNAL_PASSWORD_HASH,
                full_name=full_name,
                role=role,
                is_active=True,
                auth_source=source,
            )
            db.add(user)
            await db.flush()
            logger.info("provisioned %s user %r with role %r", source, username, role)
        else:
            user = existing
            user.role = role
            if full_name:
                user.full_name = full_name
            if email and email != user.email and await _email_available(db, email, own_id=user.id):
                user.email = email
        return user

    return None


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    username = data.username.strip()
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is not None and user.auth_source == "local":
        if not verify_password(data.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )
    else:
        # Unknown username, or an LDAP/RADIUS-backed account.
        user = await _external_login(db, username, data.password, user)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    user.last_login = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(data={"sub": str(user.id), "role": user.role})

    return TokenResponse(
        access_token=token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        user=await _user_response(db, user),
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _user_response(db, user)
