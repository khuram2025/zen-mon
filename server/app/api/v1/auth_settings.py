"""Authentication provider settings: LDAP / Active Directory and RADIUS.

Stored in the ``system_settings`` KV table (keys ``auth.ldap`` and
``auth.radius``) — no migration required, same pattern as the Security
TLS settings. Secrets are write-only: GET masks them and PUT keeps the
stored value when the field comes back blank.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.settings import _get_system_setting, _upsert_system_setting
from app.core.database import get_db
from app.core.crypto import encrypt_text, decrypt_secret
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.external_auth import (
    LDAP_DEFAULTS,
    LDAP_SETTINGS_KEY,
    RADIUS_DEFAULTS,
    RADIUS_SETTINGS_KEY,
    ExternalAuthError,
    ldap_authenticate,
    ldap_test_bind,
    map_ldap_role,
    map_radius_role,
    merge_config,
    radius_authenticate,
)

router = APIRouter(prefix="/system/auth", tags=["Authentication Settings"])


class RoleMapping(BaseModel):
    group: Optional[str] = None   # LDAP group DN or CN
    value: Optional[str] = None   # RADIUS Class / Filter-Id value
    role: str


class LdapConfigIn(BaseModel):
    enabled: bool = False
    server: str = ""
    port: int = Field(389, ge=1, le=65535)
    use_ssl: bool = False
    use_starttls: bool = True
    ca_certificate_pem: str = Field(default="", max_length=131072)
    bind_dn: str = ""
    bind_password: str = ""       # blank = keep stored
    base_dn: str = ""
    user_filter: str = "(sAMAccountName={username})"
    email_attr: str = "mail"
    name_attr: str = "displayName"
    group_attr: str = "memberOf"
    group_mappings: list[RoleMapping] = []
    default_role: str = ""
    auto_provision: bool = True


class RadiusConfigIn(BaseModel):
    enabled: bool = False
    server: str = ""
    port: int = Field(1812, ge=1, le=65535)
    secret: str = ""              # blank = keep stored
    timeout: int = Field(5, ge=1, le=60)
    retries: int = Field(3, ge=1, le=10)
    nas_identifier: str = "zenplus"
    class_mappings: list[RoleMapping] = []
    default_role: str = "viewer"
    auto_provision: bool = True


class TestCredentialsIn(BaseModel):
    username: str = ""
    password: str = ""
    # Optional unsaved form values. When present the test runs against
    # them (merged over the stored config) so an admin can verify a
    # server before committing it. Blank secrets fall back to the stored
    # secret, matching PUT semantics.
    ldap: Optional[LdapConfigIn] = None
    radius: Optional[RadiusConfigIn] = None


def _masked(cfg: dict, secret_field: str) -> dict:
    out = dict(cfg)
    out[f"has_{secret_field}"] = bool(out.get(secret_field))
    out[secret_field] = ""
    return out


async def _load(db: AsyncSession, key: str, defaults: dict) -> dict:
    return merge_config(defaults, await _get_system_setting(db, key))


def _with_override(stored: dict, override: Optional[BaseModel], secret_field: str) -> dict:
    """Stored config, replaced by unsaved form values when the caller sent
    them. A blank secret in the override keeps the stored one."""
    if override is None:
        return stored
    incoming = override.model_dump()
    if not incoming.get(secret_field):
        incoming[secret_field] = stored.get(secret_field, "")
    return merge_config(stored, incoming)


@router.get("/providers")
async def get_providers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    ldap_cfg = await _load(db, LDAP_SETTINGS_KEY, LDAP_DEFAULTS)
    radius_cfg = await _load(db, RADIUS_SETTINGS_KEY, RADIUS_DEFAULTS)
    return {
        "ldap": _masked(ldap_cfg, "bind_password"),
        "radius": _masked(radius_cfg, "secret"),
    }


async def _save_provider(
    db: AsyncSession,
    current_user: User,
    *,
    key: str,
    defaults: dict,
    incoming: dict,
    secret_field: str,
) -> dict:
    stored = await _load(db, key, defaults)
    if not incoming.get(secret_field):
        incoming[secret_field] = stored.get(secret_field, "")
    merged = merge_config(defaults, incoming)
    await write_audit_log(
        db,
        actor=current_user,
        action="settings.auth.update",
        resource_type="settings",
        resource_id=key,
        metadata={"enabled": merged["enabled"], "server": merged["server"]},
    )
    protected = dict(merged)
    protected[secret_field] = encrypt_text(merged.get(secret_field))
    await _upsert_system_setting(db, key, protected)  # commits
    return merged


@router.put("/ldap")
async def save_ldap(
    data: LdapConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if data.enabled and not (data.use_ssl or data.use_starttls):
        raise HTTPException(400, 'Enable LDAPS or StartTLS; plaintext LDAP binds are not permitted')
    if data.ca_certificate_pem:
        import ssl
        try:
            ssl.create_default_context(cadata=data.ca_certificate_pem)
        except (ssl.SSLError, ValueError):
            raise HTTPException(400, 'Invalid directory CA certificate PEM')
    payload = data.model_dump()
    payload["group_mappings"] = [
        {"group": m.group or "", "role": m.role} for m in data.group_mappings if m.role
    ]
    if data.enabled and (not data.server or not data.base_dn):
        raise HTTPException(status_code=400, detail="Server and Base DN are required to enable LDAP")
    merged = await _save_provider(
        db, current_user,
        key=LDAP_SETTINGS_KEY, defaults=LDAP_DEFAULTS,
        incoming=payload, secret_field="bind_password",
    )
    return _masked(merged, "bind_password")


@router.put("/radius")
async def save_radius(
    data: RadiusConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    payload = data.model_dump()
    payload["class_mappings"] = [
        {"value": m.value or "", "role": m.role} for m in data.class_mappings if m.role
    ]
    if data.enabled and not data.server:
        raise HTTPException(status_code=400, detail="Server is required to enable RADIUS")
    stored = await _load(db, RADIUS_SETTINGS_KEY, RADIUS_DEFAULTS)
    if data.enabled and not (data.secret or stored.get("secret")):
        raise HTTPException(status_code=400, detail="Shared secret is required to enable RADIUS")
    merged = await _save_provider(
        db, current_user,
        key=RADIUS_SETTINGS_KEY, defaults=RADIUS_DEFAULTS,
        incoming=payload, secret_field="secret",
    )
    return _masked(merged, "secret")


@router.post("/ldap/test")
async def test_ldap(
    data: TestCredentialsIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Test the LDAP settings (saved, or the unsaved form values when the
    request carries them). Without credentials, only the service bind is
    verified; with credentials, a full user authentication is performed
    and the mapped role reported."""
    cfg = _with_override(await _load(db, LDAP_SETTINGS_KEY, LDAP_DEFAULTS), data.ldap, "bind_password")
    try:
        if not data.username:
            # Bind-only check: authenticate with an unresolvable user to
            # exercise server reachability and the service bind.
            if not cfg.get("server") or not cfg.get("base_dn"):
                raise ExternalAuthError("LDAP server and base DN are required")
            await asyncio.to_thread(ldap_test_bind, cfg)
            return {"success": True, "message": "Connection and service bind OK"}

        info = await asyncio.to_thread(ldap_authenticate, cfg, data.username, data.password)
        if info is None:
            return {"success": False, "message": "User not found or invalid credentials"}
        role = map_ldap_role(cfg, info["groups"])
        return {
            "success": True,
            "message": f"Authenticated as {info['dn']}",
            "dn": info["dn"],
            "email": info["email"],
            "full_name": info["full_name"],
            "groups": info["groups"],
            "mapped_role": role,
            "would_sign_in": role is not None,
        }
    except ExternalAuthError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # ldap3 socket/TLS errors surface as a readable failure, not a 500
        return {
            "success": False,
            "message": f"Cannot reach LDAP server {cfg.get('server')}:{cfg.get('port')}: {exc}",
        }


@router.post("/radius/test")
async def test_radius(
    data: TestCredentialsIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Test the RADIUS settings (saved, or the unsaved form values when
    the request carries them) with a real Access-Request."""
    if not data.username or not data.password:
        raise HTTPException(status_code=400, detail="Username and password are required for a RADIUS test")
    cfg = _with_override(await _load(db, RADIUS_SETTINGS_KEY, RADIUS_DEFAULTS), data.radius, "secret")
    try:
        info = await asyncio.to_thread(radius_authenticate, cfg, data.username, data.password)
    except ExternalAuthError as exc:
        return {"success": False, "message": str(exc)}
    if info is None:
        return {"success": False, "message": "Access-Reject: invalid credentials"}
    role = map_radius_role(cfg, info["reply_values"])
    return {
        "success": True,
        "message": "Access-Accept",
        "reply_values": info["reply_values"],
        "mapped_role": role,
        "would_sign_in": role is not None,
    }
