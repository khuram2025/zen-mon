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
    use_starttls: bool = False
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


def _masked(cfg: dict, secret_field: str) -> dict:
    out = dict(cfg)
    out[f"has_{secret_field}"] = bool(out.get(secret_field))
    out[secret_field] = ""
    return out


async def _load(db: AsyncSession, key: str, defaults: dict) -> dict:
    return merge_config(defaults, await _get_system_setting(db, key))


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
    await _upsert_system_setting(db, key, merged)  # commits
    return merged


@router.put("/ldap")
async def save_ldap(
    data: LdapConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
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
    """Test the saved LDAP settings. Without credentials, only the
    service bind is verified; with credentials, a full user
    authentication is performed and the mapped role reported."""
    cfg = await _load(db, LDAP_SETTINGS_KEY, LDAP_DEFAULTS)
    try:
        if not data.username:
            # Bind-only check: authenticate with an unresolvable user to
            # exercise server reachability and the service bind.
            def bind_only():
                import ldap3
                use_ssl = bool(cfg.get("use_ssl"))
                server = ldap3.Server(
                    cfg["server"],
                    port=int(cfg.get("port") or (636 if use_ssl else 389)),
                    use_ssl=use_ssl, connect_timeout=10,
                )
                conn = ldap3.Connection(
                    server, user=cfg.get("bind_dn") or None,
                    password=cfg.get("bind_password") or None,
                    receive_timeout=10, auto_bind=False,
                )
                conn.open()
                if cfg.get("use_starttls") and not use_ssl:
                    conn.start_tls()
                if not conn.bind():
                    raise ExternalAuthError(
                        f"Service bind failed: {conn.result.get('description', 'invalid credentials')}"
                    )
                conn.unbind()

            if not cfg.get("server") or not cfg.get("base_dn"):
                raise ExternalAuthError("LDAP server and base DN are required")
            await asyncio.to_thread(bind_only)
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


@router.post("/radius/test")
async def test_radius(
    data: TestCredentialsIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Test the saved RADIUS settings with a real Access-Request."""
    if not data.username or not data.password:
        raise HTTPException(status_code=400, detail="Username and password are required for a RADIUS test")
    cfg = await _load(db, RADIUS_SETTINGS_KEY, RADIUS_DEFAULTS)
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
