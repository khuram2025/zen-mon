import asyncio
import hashlib
import secrets
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text

from app.core import password_policy, totp
from app.core.crypto import decrypt_secret, encrypt_text
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    get_current_user,
    get_role_permissions,
    get_role_scope_tags,
    hash_password,
    SCOPE_LOOKUP_FAILED,
    security,
    verify_password,
)
from app.core.config import get_settings
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    MfaEnableRequest,
    MfaLoginRequest,
    MfaReauthRequest,
    MfaSetupRequest,
    PasswordPolicyResponse,
    TokenResponse,
    UserResponse,
)
from app.services import auth_policy, oidc_service, session_service
from app.services.audit_service import write_audit_log
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

_MFA_CHALLENGE_MINUTES = 5
# Verified against when the username is unknown so a miss costs the same bcrypt
# round as a hit (no timing oracle for account enumeration).
_dummy_hash: Optional[str] = None


def _timing_equalizer(password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("zenplus-timing-equalizer")
    verify_password(password, _dummy_hash)


def _permissions_revision(role: str, permissions: list[str]) -> str:
    digest = hashlib.sha256((role + "|" + ",".join(sorted(permissions))).encode()).hexdigest()
    return digest[:12]


async def _user_response(db: AsyncSession, user: User, session_id: Optional[str] = None) -> UserResponse:
    resp = UserResponse.model_validate(user)
    resp.permissions = await get_role_permissions(db, user.role)
    resp.permissions_revision = _permissions_revision(user.role, resp.permissions)
    resp.role_scope_tags = [t for t in await get_role_scope_tags(db, user.role) if t != SCOPE_LOOKUP_FAILED]
    resp.must_change_password = bool(getattr(user, "must_change_password", False))
    resp.mfa_enabled = bool(getattr(user, "mfa_enabled", False))
    resp.mfa_required = (not resp.mfa_enabled) and await auth_policy.mfa_required_for(db, user)
    resp.session_id = session_id
    return resp


async def _email_available(db: AsyncSession, email: str, own_id) -> bool:
    result = await db.execute(select(User.id).where(User.email == email, User.id != own_id))
    return result.first() is None


async def _role_exists(db: AsyncSession, role_name: str) -> bool:
    row = (await db.execute(text("SELECT 1 FROM roles WHERE name = :n"), {"n": role_name})).first()
    return row is not None


async def _external_login(
    db: AsyncSession, username: str, password: str, existing: Optional[User]
) -> Optional[User]:
    """Authenticate against LDAP/RADIUS and provision or refresh the
    matching local user row. Returns None on bad credentials; raises 403
    for policy denials (provider disabled, no role mapped, provisioning
    off, mapped role missing)."""
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
                role = map_radius_role(cfg, info["reply_values"], info.get("reply_attributes"))
        except ExternalAuthError:
            logger.warning("%s authentication error for %r", source, username, exc_info=True)
            continue

        if role is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{source.upper()} sign-in succeeded, but no ZenPlus role is mapped for this account",
            )
        # A mapping can outlive its role (deleted, or a stale string). Never
        # provision or refresh an account onto a role that does not exist.
        if not await _role_exists(db, role):
            logger.error("%s mapping for %r points at missing role %r", source, username, role)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{source.upper()} sign-in succeeded, but the mapped ZenPlus role no longer exists; "
                       "ask an administrator to fix the role mapping",
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
            if user.role != role:
                logger.info("%s user %r role refreshed %r -> %r", source, username, user.role, role)
            user.role = role
            if full_name:
                user.full_name = full_name
            if email and email != user.email and await _email_available(db, email, own_id=user.id):
                user.email = email
        return user

    return None


def _client_ip(request: Optional[Request]) -> str:
    return request.client.host if request and request.client else "unknown"


async def _record_failure(db: AsyncSession, user: User, policy: dict) -> bool:
    """Count a failed credential check; lock the account at the threshold.
    Returns True when this failure locked it."""
    now = datetime.now(timezone.utc)
    user.failed_login_count = (user.failed_login_count or 0) + 1
    user.last_failed_login = now
    locked = False
    if user.failed_login_count >= policy["lockout_threshold"]:
        user.locked_until = now + timedelta(minutes=policy["lockout_minutes"])
        user.failed_login_count = 0
        locked = True
    await db.commit()
    return locked


async def _issue_session(db: AsyncSession, user: User, request: Optional[Request], *,
                         method: str, mfa_verified: bool) -> TokenResponse:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login = datetime.now(timezone.utc)
    ttl = timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    jti, _exp = await session_service.create_session(
        db, user.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent") if request else None,
        auth_method=method, mfa_verified=mfa_verified, ttl=ttl,
    )
    await db.commit()
    await db.refresh(user)
    token = create_access_token(
        data={"sub": str(user.id), "role": user.role, "jti": jti,
              "ver": getattr(user, "token_version", 0) or 0, "mfa": mfa_verified},
        expires_delta=ttl,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        user=await _user_response(db, user, session_id=jti),
    )


def _verify_mfa_code(user: User, code: str) -> tuple[bool, Optional[int], bool]:
    """(ok, matched_counter, used_recovery_code). Does not mutate the user."""
    secret = decrypt_secret(user.mfa_secret_enc) if user.mfa_secret_enc else None
    if not secret:
        return False, None, False
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) == totp.DIGITS and len(code.strip().replace(" ", "")) == totp.DIGITS:
        counter = totp.verify(secret, digits, last_counter=user.mfa_last_counter)
        return (counter is not None), counter, False
    hashed = totp.hash_recovery_code(code)
    if hashed in (user.mfa_recovery_hashes or []):
        return True, None, True
    return False, None, False


async def _complete_mfa(db: AsyncSession, user: User, code: str, policy: dict) -> None:
    """Validate the second factor or raise 401 (counting toward lockout)."""
    ok, counter, used_recovery = _verify_mfa_code(user, code)
    if not ok:
        locked = await _record_failure(db, user, policy)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code" + (" — account locked" if locked else ""),
        )
    if counter is not None:
        user.mfa_last_counter = counter
    if used_recovery:
        user.mfa_recovery_hashes = [h for h in (user.mfa_recovery_hashes or [])
                                    if h != totp.hash_recovery_code(code)]


async def _login_impl(data: LoginRequest, db: AsyncSession, request: Optional[Request]):
    username = data.username.strip()
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    policy = await auth_policy.get_policy(db)
    now = datetime.now(timezone.utc)

    if user is not None and user.auth_source == "local":
        if user.locked_until and user.locked_until > now:
            minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed sign-in attempts. Try again in about {minutes} minute(s) "
                       "or ask an administrator to unlock the account.",
                headers={"Retry-After": str(minutes * 60)},
            )
        if not verify_password(data.password, user.password_hash):
            locked = await _record_failure(db, user, policy)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password" if not locked else
                       "Too many failed sign-in attempts; the account is temporarily locked",
            )
    elif user is not None and user.auth_source == "oidc":
        _timing_equalizer(data.password)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="This account signs in with single sign-on")
    else:
        # Unknown username, or an LDAP/RADIUS-backed account.
        if user is None:
            _timing_equalizer(data.password)
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

    method = user.auth_source if user.auth_source != "local" else "password"
    if user.auth_source == "local" and user.mfa_enabled:
        if not data.otp:
            challenge = jwt.encode(
                {"sub": str(user.id), "purpose": "mfa",
                 "exp": now + timedelta(minutes=_MFA_CHALLENGE_MINUTES)},
                settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
            )
            return TokenResponse(mfa_required=True, challenge=challenge)
        await _complete_mfa(db, user, data.otp, policy)
        return await _issue_session(db, user, request, method=method, mfa_verified=True)
    return await _issue_session(db, user, request, method=method, mfa_verified=False)


@router.get("/password-policy", response_model=PasswordPolicyResponse)
async def get_password_policy():
    """The local-account password rules, so every form shows what the API enforces."""
    return PasswordPolicyResponse(**password_policy.requirements())


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db), request: Request = None):
    from app.services.sensor_rate_limit import enforce_sensor_quota
    from app.services.management_access import check_web_access
    source = _client_ip(request)
    if request:
        check_web_access(source)
        try:
            await enforce_sensor_quota('login-ip', hashlib.sha256(source.encode()).hexdigest(), amount=1, limit=30)
            await enforce_sensor_quota('login-account', hashlib.sha256(data.username.strip().lower().encode()).hexdigest(), amount=1, limit=10)
        except HTTPException as exc:
            raise HTTPException(429, 'Too many sign-in attempts; retry in one minute', headers={'Retry-After':'60'}) from exc
    try:
        result = await _login_impl(data, db, request)
    except HTTPException as exc:
        await write_audit_log(db, actor=None, action='auth.login_failed', resource_type='session',
                              metadata={'username': data.username[:100], 'source_ip': source,
                                        'reason': str(exc.detail)[:120]})
        await db.commit()
        raise
    if result.mfa_required:
        return result
    await write_audit_log(db, actor=None, action='auth.login_succeeded', resource_type='session',
                          resource_id=str(result.user.id),
                          metadata={'username': result.user.username, 'source_ip': source,
                                    'mfa': bool(result.user.mfa_enabled)})
    await db.commit()
    return result


@router.post("/login/mfa", response_model=TokenResponse)
async def login_mfa(data: MfaLoginRequest, db: AsyncSession = Depends(get_db), request: Request = None):
    """Second step: exchange the challenge from /auth/login plus an authenticator
    (or recovery) code for a session."""
    from app.services.sensor_rate_limit import enforce_sensor_quota
    source = _client_ip(request)
    try:
        payload = jwt.decode(data.challenge, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "mfa":
            raise jwt.InvalidTokenError("wrong purpose")
        user_id = payload["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Sign-in expired; start again")
    try:
        await enforce_sensor_quota('login-mfa', hashlib.sha256(str(user_id).encode()).hexdigest(), amount=1, limit=10)
    except HTTPException as exc:
        raise HTTPException(429, 'Too many attempts; retry in one minute', headers={'Retry-After': '60'}) from exc
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active or not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="Sign-in expired; start again")
    now = datetime.now(timezone.utc)
    if user.locked_until and user.locked_until > now:
        raise HTTPException(status_code=429, detail="Account temporarily locked", headers={"Retry-After": "60"})
    policy = await auth_policy.get_policy(db)
    try:
        await _complete_mfa(db, user, data.code, policy)
    except HTTPException as exc:
        await write_audit_log(db, actor=None, action='auth.login_failed', resource_type='session',
                              metadata={'username': user.username, 'source_ip': source, 'reason': 'mfa'})
        await db.commit()
        raise
    result = await _issue_session(db, user, request, method="password", mfa_verified=True)
    await write_audit_log(db, actor=None, action='auth.login_succeeded', resource_type='session',
                          resource_id=str(user.id),
                          metadata={'username': user.username, 'source_ip': source, 'mfa': True})
    await db.commit()
    return result


@router.post('/logout')
async def logout(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await session_service.revoke_all_sessions(db, user.id, by=user.username, reason="sign out")
    await write_audit_log(db, actor=user, action='auth.logout', resource_type='session', metadata={'all_sessions': True})
    await db.commit()
    return {'message': 'All sessions signed out'}


def _session_id_from(credentials) -> Optional[str]:
    try:
        payload = jwt.decode(credentials.credentials, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload.get("jti")
    except Exception:
        return None


@router.post("/stream-ticket")
async def stream_ticket(
    user: User = Depends(get_current_user),
    credentials=Depends(security),
):
    """A one-minute ticket the browser appends to an EventSource URL
    (``?ticket=``) so the real session token never appears in a URL."""
    from app.core.security import STREAM_TICKET_SECONDS, create_stream_ticket
    return {"ticket": create_stream_ticket(user, _session_id_from(credentials)),
            "expires_in": STREAM_TICKET_SECONDS}


@router.get("/me", response_model=UserResponse)
async def get_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    credentials=Depends(security),
):
    return await _user_response(db, user, session_id=_session_id_from(credentials))


# ── Multi-factor authentication (TOTP) for local accounts ────────────────────

def _require_local(user: User) -> None:
    if getattr(user, "auth_source", "local") != "local":
        raise HTTPException(
            status_code=400,
            detail=f"Your account authenticates via {user.auth_source.upper()}; "
                   "multi-factor authentication is managed by that provider",
        )


@router.get("/mfa/status")
async def mfa_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return {
        "supported": user.auth_source == "local",
        "enabled": bool(user.mfa_enabled),
        "enrolled_at": user.mfa_enrolled_at,
        "recovery_codes_remaining": len(user.mfa_recovery_hashes or []),
        "pending_setup": bool(user.mfa_secret_enc and not user.mfa_enabled),
        "required": await auth_policy.mfa_required_for(db, user),
    }


def _require_password_changed(user: User) -> None:
    """MFA cannot be set up while the account still owes a password change:
    whoever holds a temporary or seeded password must not be able to bind
    their own authenticator before the owner sets a real password."""
    if getattr(user, "must_change_password", False):
        raise HTTPException(status_code=403, detail={
            "code": "password_change_required",
            "message": "You must change your password before continuing",
        })


@router.post("/mfa/setup")
async def mfa_setup(
    data: MfaSetupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start enrolment: returns a new secret and an otpauth:// URI for an
    authenticator app. MFA is not active until /mfa/enable confirms a code."""
    _require_local(user)
    _require_password_changed(user)
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Multi-factor authentication is already enabled")
    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")
    secret = totp.generate_secret()
    user.mfa_secret_enc = encrypt_text(secret)
    await db.commit()
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user.username),
            "issuer": "ZenPlus", "account": user.username, "digits": totp.DIGITS, "period": totp.STEP_SECONDS}


@router.post("/mfa/enable")
async def mfa_enable(
    data: MfaEnableRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_local(user)
    _require_password_changed(user)
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Multi-factor authentication is already enabled")
    secret = decrypt_secret(user.mfa_secret_enc) if user.mfa_secret_enc else None
    if not secret:
        raise HTTPException(status_code=400, detail="Start setup first")
    counter = totp.verify(secret, data.code)
    if counter is None:
        raise HTTPException(status_code=400, detail="That code is not valid; check your device clock and try again")
    codes = totp.new_recovery_codes()
    user.mfa_enabled = True
    user.mfa_enrolled_at = datetime.now(timezone.utc)
    user.mfa_last_counter = counter
    user.mfa_recovery_hashes = [totp.hash_recovery_code(c) for c in codes]
    await write_audit_log(db, actor=user, action="auth.mfa_enabled", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username})
    await db.commit()
    return {"enabled": True, "recovery_codes": codes,
            "message": "Store these recovery codes somewhere safe; each works once and they are not shown again."}


async def _reauth(user: User, data: MfaReauthRequest, db: AsyncSession) -> None:
    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")
    policy = await auth_policy.get_policy(db)
    await _complete_mfa(db, user, data.code, policy)


@router.post("/mfa/disable")
async def mfa_disable(
    data: MfaReauthRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_local(user)
    if not user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Multi-factor authentication is not enabled")
    if await auth_policy.mfa_required_for(db, user):
        raise HTTPException(status_code=400, detail="Multi-factor authentication is required for your account by policy")
    await _reauth(user, data, db)
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_recovery_hashes = []
    user.mfa_enrolled_at = None
    user.mfa_last_counter = None
    await write_audit_log(db, actor=user, action="auth.mfa_disabled", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username})
    await db.commit()
    return {"enabled": False}


@router.post("/mfa/recovery-codes")
async def mfa_regenerate_recovery_codes(
    data: MfaReauthRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_local(user)
    if not user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Multi-factor authentication is not enabled")
    await _reauth(user, data, db)
    codes = totp.new_recovery_codes()
    user.mfa_recovery_hashes = [totp.hash_recovery_code(c) for c in codes]
    await write_audit_log(db, actor=user, action="auth.mfa_recovery_regenerated", resource_type="user",
                          resource_id=str(user.id), metadata={"username": user.username})
    await db.commit()
    return {"recovery_codes": codes}


# ── OpenID Connect single sign-on ────────────────────────────────────────────

_OIDC_COOKIE = "zp_oidc"
_exchange_memory: dict[str, tuple[float, str]] = {}
_EXCHANGE_SECONDS = 60


async def _store_exchange(payload: str) -> str:
    """One-time code the SPA swaps for the session token, so the token itself
    never travels in a URL. Redis when available (multi-worker safe)."""
    code = secrets.token_urlsafe(32)
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        await r.set(f"zp:sso:{code}", payload, ex=_EXCHANGE_SECONDS)
        await r.close()
        return code
    except Exception:
        now = time.monotonic()
        for k in [k for k, (t, _) in _exchange_memory.items() if now - t > _EXCHANGE_SECONDS]:
            _exchange_memory.pop(k, None)
        _exchange_memory[code] = (now, payload)
        return code


async def _pop_exchange(code: str) -> Optional[str]:
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        pipe = r.pipeline()
        pipe.get(f"zp:sso:{code}")
        pipe.delete(f"zp:sso:{code}")
        value, _ = await pipe.execute()
        await r.close()
        if value is not None:
            return value.decode() if isinstance(value, bytes) else value
    except Exception:
        pass
    hit = _exchange_memory.pop(code, None)
    if hit and time.monotonic() - hit[0] <= _EXCHANGE_SECONDS:
        return hit[1]
    return None


async def _oidc_config(db: AsyncSession) -> dict:
    from app.api.v1.settings import _get_system_setting
    return oidc_service.merge_oidc(await _get_system_setting(db, oidc_service.OIDC_SETTINGS_KEY))


def _request_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def _sso_failure(message: str) -> RedirectResponse:
    from urllib.parse import quote
    resp = RedirectResponse(f"/login?sso_error={quote(message[:200])}", status_code=303)
    resp.delete_cookie(_OIDC_COOKIE, path="/api/v1/auth/oidc")
    return resp


@router.get("/oidc/config")
async def oidc_public_config(db: AsyncSession = Depends(get_db)):
    """What the sign-in page needs: whether to show the SSO button."""
    cfg = await _oidc_config(db)
    ready = bool(cfg["enabled"] and cfg["issuer"] and cfg["client_id"])
    return {"enabled": ready, "display_name": cfg["display_name"] if ready else None}


@router.get("/oidc/login")
async def oidc_login(request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.management_access import check_web_access
    check_web_access(_client_ip(request))
    cfg = await _oidc_config(db)
    if not (cfg["enabled"] and cfg["issuer"] and cfg["client_id"]):
        return _sso_failure("Single sign-on is not enabled")
    st = oidc_service.new_login_state()
    try:
        url = await oidc_service.authorization_url(cfg, oidc_service.redirect_uri(cfg, _request_base(request)), st)
    except oidc_service.OidcError as exc:
        return _sso_failure(str(exc))
    cookie = jwt.encode(
        {"state": st["state"], "nonce": st["nonce"], "verifier": st["verifier"],
         "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(_OIDC_COOKIE, cookie, max_age=600, httponly=True, samesite="lax",
                    secure=_request_base(request).startswith("https"), path="/api/v1/auth/oidc")
    return resp


async def _oidc_user(db: AsyncSession, cfg: dict, ident: dict, role: str) -> User:
    existing = (await db.execute(select(User).where(User.auth_source == "oidc", User.external_id == ident["sub"]))).scalar_one_or_none()
    if existing is not None:
        user = existing
        user.role = role
        if ident["full_name"]:
            user.full_name = str(ident["full_name"])[:255]
        email = (str(ident["email"]).strip().lower() if ident["email"] else None)
        if email and email != user.email and await _email_available(db, email, own_id=user.id):
            user.email = email
        return user
    if not cfg.get("auto_provision"):
        raise oidc_service.OidcError("Automatic account creation is disabled and no ZenPlus account is linked to this identity")
    clash = (await db.execute(select(User.id).where(func.lower(User.username) == ident["username"].lower()))).first()
    if clash:
        raise oidc_service.OidcError(
            f"A ZenPlus account named '{ident['username']}' already exists and is not linked to this identity "
            "provider; ask an administrator to resolve the conflict")
    email = (str(ident["email"]).strip().lower() if ident["email"] else None) or f"{ident['username']}@oidc.external"
    if not await _email_available(db, email, own_id=None):
        email = f"{ident['username']}@oidc.external"
    user = User(username=ident["username"], email=email, password_hash=EXTERNAL_PASSWORD_HASH,
                full_name=(str(ident["full_name"])[:255] if ident["full_name"] else None), role=role,
                is_active=True, auth_source="oidc", external_id=ident["sub"])
    db.add(user)
    await db.flush()
    logger.info("provisioned oidc user %r with role %r", user.username, role)
    return user


@router.get("/oidc/callback")
async def oidc_callback(request: Request, db: AsyncSession = Depends(get_db),
                        code: Optional[str] = None, state: Optional[str] = None,
                        error: Optional[str] = None, error_description: Optional[str] = None):
    source = _client_ip(request)
    try:
        cfg = await _oidc_config(db)
        if not (cfg["enabled"] and cfg["issuer"] and cfg["client_id"]):
            raise oidc_service.OidcError("Single sign-on is not enabled")
        if error:
            raise oidc_service.OidcError(f"The identity provider reported: {error_description or error}")
        try:
            saved = jwt.decode(request.cookies.get(_OIDC_COOKIE, ""), settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        except jwt.PyJWTError:
            raise oidc_service.OidcError("The sign-in session expired; start again")
        if not state or not code or not secrets.compare_digest(state, saved.get("state", "")):
            raise oidc_service.OidcError("The sign-in response does not match this browser session; start again")
        redirect = oidc_service.redirect_uri(cfg, _request_base(request))
        tokens = await oidc_service.exchange_code(cfg, redirect, code, saved["verifier"])
        claims = await oidc_service.validate_id_token(cfg, tokens["id_token"], saved["nonce"])
        ident = oidc_service.identity(cfg, claims)
        role, _rule = oidc_service.explain_group_role(cfg, ident["groups"])
        if role is None:
            raise oidc_service.OidcError("Sign-in succeeded, but no ZenPlus role is mapped for this account's groups")
        if not await _role_exists(db, role):
            raise oidc_service.OidcError("The mapped ZenPlus role no longer exists; ask an administrator to fix the role mapping")
        user = await _oidc_user(db, cfg, ident, role)
        if not user.is_active:
            raise oidc_service.OidcError("This account is disabled")
        result = await _issue_session(db, user, request, method="oidc", mfa_verified=False)
        await write_audit_log(db, actor=None, action='auth.login_succeeded', resource_type='session',
                              resource_id=str(user.id), metadata={'username': user.username, 'source_ip': source, 'method': 'oidc'})
        await db.commit()
    except oidc_service.OidcError as exc:
        await db.rollback()
        await write_audit_log(db, actor=None, action='auth.login_failed', resource_type='session',
                              metadata={'source_ip': source, 'method': 'oidc', 'reason': str(exc)[:160]})
        await db.commit()
        return _sso_failure(str(exc))
    code_out = await _store_exchange(result.model_dump_json())
    resp = RedirectResponse(f"/login?sso={code_out}", status_code=303)
    resp.delete_cookie(_OIDC_COOKIE, path="/api/v1/auth/oidc")
    return resp


@router.post("/oidc/exchange", response_model=TokenResponse)
async def oidc_exchange(data: dict):
    """Swap the one-time code from the SSO redirect for the session token."""
    payload = await _pop_exchange(str(data.get("code", ""))[:128])
    if payload is None:
        raise HTTPException(status_code=400, detail="This sign-in link has expired; start again")
    return TokenResponse.model_validate_json(payload)
