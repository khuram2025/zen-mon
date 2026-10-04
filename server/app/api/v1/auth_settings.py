"""Authentication provider settings: LDAP / Active Directory and RADIUS.

Stored in the ``system_settings`` KV table (keys ``auth.ldap`` and
``auth.radius``) — no migration required, same pattern as the Security
TLS settings. Secrets are write-only: GET masks them and PUT keeps the
stored value when the field comes back blank (an explicit ``clear_secret``
flag removes it).

Every configuration carries a ``revision``; a stale full-form save is refused
(409), enabling / disabling goes through a narrow endpoint that cannot touch
any other field, and every change is audited with a redacted before/after diff.
Role mappings are validated against real roles, and pointing a mapping (or the
default) at a privileged role needs an explicit confirmation.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.settings import _get_system_setting
from app.core.database import get_db
from app.core.crypto import encrypt_text
from app.core.permissions import ACCESS_CONTROL_PERMISSIONS, effective_permissions
from app.core.security import require_admin_user
from app.models.user import User
from app.services import auth_policy, session_service
from app.services.audit_service import write_audit_log
from app.services import oidc_service
from app.services.external_auth import (
    LDAP_DEFAULTS,
    LDAP_SETTINGS_KEY,
    RADIUS_ATTRIBUTES,
    RADIUS_DEFAULTS,
    RADIUS_MAX_TOTAL_SECONDS,
    RADIUS_SETTINGS_KEY,
    ExternalAuthError,
    explain_ldap_role,
    explain_radius_role,
    ldap_authenticate,
    ldap_test_bind,
    merge_config,
    radius_authenticate,
)

router = APIRouter(prefix="/system/auth", tags=["Authentication Settings"])

_PROVIDERS = {
    "ldap": {"key": LDAP_SETTINGS_KEY, "defaults": LDAP_DEFAULTS, "secret": "bind_password",
             "list": "group_mappings", "match": "group", "label": "LDAP / Active Directory"},
    "radius": {"key": RADIUS_SETTINGS_KEY, "defaults": RADIUS_DEFAULTS, "secret": "secret",
               "list": "class_mappings", "match": "value", "label": "RADIUS"},
    "oidc": {"key": oidc_service.OIDC_SETTINGS_KEY, "defaults": oidc_service.OIDC_DEFAULTS, "secret": "client_secret",
             "list": "group_mappings", "match": "group", "label": "OpenID Connect"},
}
_META_FIELDS = ("revision", "updated_at", "updated_by")


class RoleMapping(BaseModel):
    group: Optional[str] = Field(None, max_length=1024)   # LDAP group DN or CN
    value: Optional[str] = Field(None, max_length=1024)   # RADIUS reply value
    attribute: Optional[str] = Field(None, max_length=32)  # RADIUS attribute the value must come from
    role: str = ""


class _ProviderIn(BaseModel):
    # Optimistic concurrency: the revision the form was loaded from.
    expected_revision: Optional[int] = None
    # Explicitly remove the stored secret (blank = keep, never clears by accident).
    clear_secret: bool = False
    # Acknowledge that a mapping / default now grants a privileged role.
    confirm_privileged: bool = False


class LdapConfigIn(_ProviderIn):
    enabled: bool = False
    server: str = Field("", max_length=255)
    port: int = Field(389, ge=1, le=65535)
    use_ssl: bool = False
    use_starttls: bool = True
    ca_certificate_pem: str = Field(default="", max_length=131072)
    bind_dn: str = Field("", max_length=1024)
    bind_password: str = Field("", max_length=1024)       # blank = keep stored
    base_dn: str = Field("", max_length=1024)
    user_filter: str = Field("(sAMAccountName={username})", max_length=1024)
    email_attr: str = "mail"
    name_attr: str = "displayName"
    group_attr: str = "memberOf"
    group_mappings: list[RoleMapping] = []
    default_role: str = ""
    auto_provision: bool = True


class RadiusConfigIn(_ProviderIn):
    enabled: bool = False
    server: str = Field("", max_length=255)
    port: int = Field(1812, ge=1, le=65535)
    secret: str = Field("", max_length=1024)              # blank = keep stored
    timeout: int = Field(5, ge=1, le=60)
    retries: int = Field(3, ge=1, le=10)
    nas_identifier: str = Field("zenplus", max_length=64)
    class_mappings: list[RoleMapping] = []
    default_role: str = ""
    auto_provision: bool = True

    @model_validator(mode="after")
    def _bounded_budget(self):
        if self.timeout * self.retries > RADIUS_MAX_TOTAL_SECONDS:
            raise ValueError(
                f"Timeout × retries must be {RADIUS_MAX_TOTAL_SECONDS} seconds or less "
                f"(currently {self.timeout * self.retries})")
        return self


class OidcConfigIn(_ProviderIn):
    enabled: bool = False
    display_name: str = Field("Single sign-on", max_length=60)
    issuer: str = Field("", max_length=512)
    client_id: str = Field("", max_length=512)
    client_secret: str = Field("", max_length=2048)        # blank = keep stored
    scopes: str = Field("openid profile email", max_length=256)
    username_claim: str = Field("preferred_username", max_length=64)
    email_claim: str = Field("email", max_length=64)
    name_claim: str = Field("name", max_length=64)
    groups_claim: str = Field("groups", max_length=64)
    group_mappings: list[RoleMapping] = []
    default_role: str = ""
    auto_provision: bool = True
    ca_certificate_pem: str = Field(default="", max_length=131072)
    redirect_base_url: str = Field("", max_length=512)


class TestCredentialsIn(BaseModel):
    username: str = ""
    password: str = ""
    # Optional unsaved form values. When present the test runs against
    # them (merged over the stored config) so an admin can verify a
    # server before committing it. Blank secrets fall back to the stored
    # secret, matching PUT semantics.
    ldap: Optional[LdapConfigIn] = None
    radius: Optional[RadiusConfigIn] = None
    oidc: Optional[OidcConfigIn] = None


class EnabledIn(BaseModel):
    enabled: bool
    expected_revision: Optional[int] = None
    # When disabling: also sign out every account that authenticates through it.
    revoke_sessions: bool = False


class PolicyIn(BaseModel):
    mfa_required: Literal["off", "admins", "all_local"] = "off"
    lockout_threshold: int = Field(10, ge=3, le=100)
    lockout_minutes: int = Field(15, ge=1, le=1440)


# ── helpers ───────────────────────────────────────────────────────────────────

def _provider(name: str) -> dict:
    spec = _PROVIDERS.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return spec


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
    incoming = override.model_dump(exclude={"expected_revision", "clear_secret", "confirm_privileged"})
    incoming["group_mappings" if "group_mappings" in incoming else "class_mappings"] = [
        m for m in incoming.get("group_mappings", incoming.get("class_mappings", [])) if m.get("role")
    ]
    if not incoming.get(secret_field):
        incoming[secret_field] = stored.get(secret_field, "")
    return merge_config(stored, incoming)


async def _store(db: AsyncSession, key: str, cfg: dict, secret_field: str) -> None:
    """Persist (no commit: the caller commits together with the audit event)."""
    protected = dict(cfg)
    protected[secret_field] = encrypt_text(cfg.get(secret_field))
    await db.execute(
        text("INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb)"),
        {"key": key, "value": json.dumps(protected)},
    )


async def _existing_roles(db: AsyncSession) -> dict[str, set[str]]:
    rows = (await db.execute(text("SELECT name, permissions FROM roles"))).all()
    return {r[0]: set(effective_permissions(r[1] if isinstance(r[1], list) else [])) for r in rows}


def _privileged(perms: set[str]) -> bool:
    return bool(perms & ACCESS_CONTROL_PERMISSIONS)


async def _validate_mappings(db: AsyncSession, spec: dict, mappings: list[dict], default_role: str,
                             roles: dict[str, set[str]]) -> list[dict]:
    """Return the cleaned mapping list or raise 422/400 with row-level messages."""
    match_key = spec["match"]
    problems: list[str] = []
    cleaned: list[dict] = []
    seen: dict[tuple, int] = {}
    for i, m in enumerate(mappings, start=1):
        match = (m.get(match_key) or "").strip()
        role = (m.get("role") or "").strip()
        attr = (m.get("attribute") or "").strip() or None
        if not match and not role:
            continue  # a wholly blank row is just an unused placeholder
        if not match:
            problems.append(f"Rule {i}: enter the {'group' if match_key == 'group' else 'value'} to match")
            continue
        if not role:
            problems.append(f"Rule {i}: choose the role to grant")
            continue
        if role not in roles:
            problems.append(f"Rule {i}: role '{role}' does not exist")
            continue
        if attr and attr not in RADIUS_ATTRIBUTES:
            problems.append(f"Rule {i}: unknown RADIUS attribute '{attr}'")
            continue
        key = (match.lower(), (attr or "").lower())
        if key in seen:
            problems.append(f"Rule {i} duplicates rule {seen[key]} ('{match}'); the later rule can never match")
            continue
        seen[key] = i
        row = {match_key: match, "role": role}
        if spec["list"] == "class_mappings":
            row["attribute"] = attr or ""
        cleaned.append(row)
    if default_role and default_role not in roles:
        problems.append(f"The default role '{default_role}' does not exist")
    if problems:
        raise HTTPException(status_code=422, detail={"code": "invalid_role_mapping", "problems": problems,
                                                     "message": problems[0]})
    return cleaned


def _target_roles(cfg: dict, spec: dict) -> set[str]:
    out = {m.get("role") for m in cfg.get(spec["list"]) or [] if m.get("role")}
    if cfg.get("default_role"):
        out.add(cfg["default_role"])
    return out


def _redacted_view(cfg: dict, spec: dict) -> dict:
    """Security-relevant fields only; secrets and certificate bodies are reduced
    to a fingerprint / presence so the audit record can never leak them."""
    view = {k: v for k, v in cfg.items() if k not in (spec["secret"], "ca_certificate_pem", *_META_FIELDS)}
    pem = cfg.get("ca_certificate_pem") or ""
    if "ca_certificate_pem" in cfg:
        view["ca_certificate_sha256"] = hashlib.sha256(pem.encode()).hexdigest()[:16] if pem else ""
    return view


def _diff(old: dict, new: dict) -> dict:
    out = {}
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out[k] = {"from": old.get(k), "to": new.get(k)}
    return out


async def _account_counts(db: AsyncSession, source: str) -> dict:
    row = (await db.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE is_active) FROM users WHERE auth_source = :s"), {"s": source})).first()
    sessions = (await db.execute(text(
        "SELECT count(*) FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE u.auth_source = :s AND s.revoked_at IS NULL AND s.expires_at > NOW()"), {"s": source})).scalar() or 0
    return {"accounts": int((row[0] if row else 0) or 0), "active_accounts": int((row[1] if row else 0) or 0),
            "open_sessions": int(sessions)}


async def _last_test(db: AsyncSession, provider: str) -> Optional[dict]:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": f"auth.test.{provider}"})).first()
    return row[0] if row and isinstance(row[0], dict) else None


async def _remember_test(db: AsyncSession, provider: str, user: User, ok: bool, message: str, server: str) -> None:
    value = {"at": datetime.now(timezone.utc).isoformat(), "ok": ok, "message": message[:300],
             "server": server, "by": user.username}
    await db.execute(
        text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb)"),
        {"k": f"auth.test.{provider}", "v": json.dumps(value)})
    await db.commit()


def _enable_requirements(provider: str, cfg: dict) -> None:
    if provider == "oidc":
        try:
            oidc_service.check_issuer_url(cfg.get("issuer") or "")
        except oidc_service.OidcError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not cfg.get("client_id"):
            raise HTTPException(status_code=400, detail="Client ID is required to enable single sign-on")
        if not cfg.get("client_secret"):
            raise HTTPException(status_code=400, detail="Client secret is required to enable single sign-on")
        return
    if provider == "ldap":
        if not (cfg.get("use_ssl") or cfg.get("use_starttls")):
            raise HTTPException(400, 'Enable LDAPS or StartTLS; plaintext LDAP binds are not permitted')
        if not cfg.get("server") or not cfg.get("base_dn"):
            raise HTTPException(status_code=400, detail="Server and Base DN are required to enable LDAP")
    else:
        if not cfg.get("server"):
            raise HTTPException(status_code=400, detail="Server is required to enable RADIUS")
        if not cfg.get("secret"):
            raise HTTPException(status_code=400, detail="Shared secret is required to enable RADIUS")


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("/providers")
async def get_providers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    out = {}
    for name, spec in _PROVIDERS.items():
        cfg = await _load(db, spec["key"], spec["defaults"])
        masked = _masked(cfg, spec["secret"])
        masked["last_test"] = await _last_test(db, name)
        masked.update(await _account_counts(db, name))
        out[name] = masked
    return out


@router.get("/policy")
async def get_policy(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin_user)):
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": auth_policy.POLICY_KEY})).first()
    policy = auth_policy.normalize(row[0] if row else None)
    admins = (await db.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE u.mfa_enabled) FROM users u JOIN roles r ON r.name = u.role "
        "WHERE u.auth_source = 'local' AND u.is_active AND r.permissions::jsonb @> '[\"system.admin\"]'::jsonb"
    ))).first()
    local = (await db.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE mfa_enabled) FROM users WHERE auth_source = 'local' AND is_active"
    ))).first()
    policy["coverage"] = {"admins": int(admins[0] or 0), "admins_with_mfa": int(admins[1] or 0),
                          "local_accounts": int(local[0] or 0), "local_with_mfa": int(local[1] or 0)}
    return policy


@router.put("/policy")
async def put_policy(
    data: PolicyIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if data.mfa_required != "off" and current_user.auth_source == "local" and not current_user.mfa_enabled:
        # Requiring MFA before the administrator has it would lock them out of
        # this very screen; they must enrol first.
        raise HTTPException(status_code=409, detail={
            "code": "enable_own_mfa_first",
            "message": "Set up multi-factor authentication on your own account before requiring it for others."})
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": auth_policy.POLICY_KEY})).first()
    old = auth_policy.normalize(row[0] if row else None)
    new = {**old, **data.model_dump(), "revision": int(old.get("revision") or 0) + 1}
    await db.execute(
        text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb)"),
        {"k": auth_policy.POLICY_KEY, "v": json.dumps(new)})
    await write_audit_log(db, actor=current_user, action="settings.auth.policy", resource_type="settings",
                          resource_id=auth_policy.POLICY_KEY, metadata=_diff(old, new))
    await db.commit()
    auth_policy.invalidate()
    return await get_policy(db, current_user)


@router.get("/history")
async def provider_history(
    provider: Optional[Literal["ldap", "radius", "oidc", "policy"]] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Configuration changes with their redacted diffs."""
    keys = {"ldap": LDAP_SETTINGS_KEY, "radius": RADIUS_SETTINGS_KEY, "oidc": oidc_service.OIDC_SETTINGS_KEY,
            "policy": auth_policy.POLICY_KEY}
    where = "action LIKE 'settings.auth.%'"
    params: dict = {"lim": limit}
    if provider:
        where += " AND resource_id = :rid"
        params["rid"] = keys[provider]
    rows = (await db.execute(text(
        f"SELECT created_at, action, actor_username, resource_id, metadata FROM audit_logs WHERE {where} "
        "ORDER BY created_at DESC LIMIT :lim"), params)).all()
    return {"data": [{"at": r[0], "action": r[1], "actor": r[2], "provider": (r[3] or "").replace("auth.", ""),
                      "details": r[4]} for r in rows]}


@router.get("/{provider}/impact")
async def provider_impact(
    provider: Literal["ldap", "radius", "oidc"],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """What disabling / changing this provider affects."""
    _provider(provider)
    return await _account_counts(db, provider)


# ── write ─────────────────────────────────────────────────────────────────────

async def _save(db: AsyncSession, actor: User, provider: str, data: _ProviderIn) -> dict:
    spec = _provider(provider)
    secret_field = spec["secret"]
    stored = await _load(db, spec["key"], spec["defaults"])
    stored_revision = int(stored.get("revision") or 0)
    if data.expected_revision is not None and data.expected_revision != stored_revision:
        raise HTTPException(status_code=409, detail={
            "code": "provider_revision_conflict",
            "message": "This configuration was changed by another administrator since you opened it. "
                       "Reload to see the latest settings.",
            "current": _masked(stored, secret_field)})

    incoming = data.model_dump(exclude={"expected_revision", "clear_secret", "confirm_privileged"})
    secret_change = "unchanged"
    if data.clear_secret:
        if incoming.get(secret_field):
            raise HTTPException(status_code=400, detail="Provide either a new secret or clear_secret, not both")
        incoming[secret_field] = ""
        secret_change = "cleared"
    elif incoming.get(secret_field):
        secret_change = "replaced"
    else:
        incoming[secret_field] = stored.get(secret_field, "")

    roles = await _existing_roles(db)
    incoming[spec["list"]] = await _validate_mappings(db, spec, incoming.get(spec["list"]) or [],
                                                      incoming.get("default_role") or "", roles)

    if incoming.get("enabled"):
        _enable_requirements(provider, {**incoming})

    # Confirm new grants of privileged roles.
    new_targets = _target_roles(incoming, spec) - _target_roles(stored, spec)
    privileged_new = sorted(r for r in new_targets if _privileged(roles.get(r, set())))
    if privileged_new and not data.confirm_privileged:
        raise HTTPException(status_code=409, detail={
            "code": "privileged_mapping_confirmation_required",
            "message": "This configuration grants privileged access to directory users: "
                       + ", ".join(privileged_new) + ". Confirm to continue.",
            "roles": privileged_new})

    merged = merge_config(spec["defaults"], incoming)
    merged["revision"] = stored_revision + 1
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged["updated_by"] = actor.username
    old_view, new_view = _redacted_view(stored, spec), _redacted_view(merged, spec)
    await _store(db, spec["key"], merged, secret_field)
    await write_audit_log(
        db, actor=actor, action="settings.auth.update", resource_type="settings", resource_id=spec["key"],
        metadata={"enabled": merged["enabled"], "server": merged.get("server") or merged.get("issuer"), "revision": merged["revision"],
                  "secret": secret_change, "privileged_grants": privileged_new,
                  "changes": _diff(old_view, new_view)})
    await db.commit()
    return merged


@router.put("/ldap")
async def save_ldap(
    data: LdapConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if data.ca_certificate_pem:
        import ssl
        try:
            ssl.create_default_context(cadata=data.ca_certificate_pem)
        except (ssl.SSLError, ValueError):
            raise HTTPException(400, 'Invalid directory CA certificate PEM')
    if data.enabled and not (data.use_ssl or data.use_starttls):
        raise HTTPException(400, 'Enable LDAPS or StartTLS; plaintext LDAP binds are not permitted')
    merged = await _save(db, current_user, "ldap", data)
    return _masked(merged, "bind_password")


@router.put("/radius")
async def save_radius(
    data: RadiusConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    merged = await _save(db, current_user, "radius", data)
    return _masked(merged, "secret")


@router.put("/oidc")
async def save_oidc(
    data: OidcConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if data.issuer:
        try:
            data.issuer = oidc_service.check_issuer_url(data.issuer)
        except oidc_service.OidcError as exc:
            raise HTTPException(400, str(exc))
    if data.ca_certificate_pem:
        import ssl
        try:
            ssl.create_default_context(cadata=data.ca_certificate_pem)
        except (ssl.SSLError, ValueError):
            raise HTTPException(400, 'Invalid CA certificate PEM')
    if data.redirect_base_url and not data.redirect_base_url.startswith(("https://", "http://")):
        raise HTTPException(400, "The redirect base URL must start with https:// (or http:// for a lab)")
    merged = await _save(db, current_user, "oidc", data)
    oidc_service.clear_caches()
    return _masked(merged, "client_secret")


@router.post("/{provider}/enabled")
async def set_provider_enabled(
    provider: Literal["ldap", "radius", "oidc"],
    data: EnabledIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Enable or disable a provider. Touches ONLY ``enabled`` - no other stored
    field can be reverted by a stale browser. Disabling blocks new sign-ins; with
    ``revoke_sessions`` it also signs out the accounts that use it."""
    spec = _provider(provider)
    stored = await _load(db, spec["key"], spec["defaults"])
    revision = int(stored.get("revision") or 0)
    if data.expected_revision is not None and data.expected_revision != revision:
        raise HTTPException(status_code=409, detail={
            "code": "provider_revision_conflict",
            "message": "This provider was changed by another administrator. Reload and try again."})
    if data.enabled:
        _enable_requirements(provider, stored)
    revoked = 0
    if stored.get("enabled") != data.enabled:
        stored["enabled"] = data.enabled
        stored["revision"] = revision + 1
        stored["updated_at"] = datetime.now(timezone.utc).isoformat()
        stored["updated_by"] = current_user.username
        await _store(db, spec["key"], stored, spec["secret"])
        if not data.enabled and data.revoke_sessions:
            ids = (await db.execute(text("SELECT id FROM users WHERE auth_source = :s"), {"s": provider})).scalars().all()
            for uid in ids:
                revoked += await session_service.revoke_all_sessions(
                    db, uid, by=current_user.username, reason=f"{provider} provider disabled")
    await write_audit_log(
        db, actor=current_user, action="settings.auth.toggle", resource_type="settings", resource_id=spec["key"],
        metadata={"enabled": data.enabled, "revoke_sessions": data.revoke_sessions, "sessions_revoked": revoked,
                  "revision": stored.get("revision")})
    await db.commit()
    return {**_masked(stored, spec["secret"]), "sessions_revoked": revoked, **await _account_counts(db, provider)}


# ── tests ─────────────────────────────────────────────────────────────────────

async def _admission(db: AsyncSession, provider: str, cfg: dict, username: str, role: Optional[str],
                     roles: dict[str, set[str]]) -> dict:
    """Would ZenPlus admit this directory user? A read-only evaluation of the same
    policy the sign-in path applies; it never provisions or updates anything."""
    label = _PROVIDERS[provider]["label"]
    reasons: list[dict] = []
    if not cfg.get("enabled"):
        reasons.append({"code": "provider_disabled",
                        "message": f"{label} is not enabled, so its users cannot sign in yet"})
    existing = (await db.execute(text("SELECT auth_source, is_active FROM users WHERE username = :u"),
                                 {"u": username.strip()})).first()
    if existing is not None:
        if existing[0] != provider:
            reasons.append({"code": "source_collision",
                            "message": f"A {existing[0].upper()} account named '{username}' already exists and "
                                       f"takes precedence; this {label} user cannot sign in as it"})
        elif not existing[1]:
            reasons.append({"code": "account_disabled", "message": "The ZenPlus account for this user is disabled"})
    elif not cfg.get("auto_provision"):
        reasons.append({"code": "provisioning_disabled",
                        "message": "Automatic account creation is off and no ZenPlus account exists for this user"})
    if role is None:
        reasons.append({"code": "no_role_mapped",
                        "message": "No role mapping matches this user and there is no default role"})
    elif role not in roles:
        reasons.append({"code": "role_missing", "message": f"The mapped role '{role}' does not exist"})
    return {"would_sign_in": not reasons, "admission_reasons": reasons,
            "existing_account": existing is not None,
            "mapped_role_privileged": bool(role and _privileged(roles.get(role, set())))}


async def _run_blocking(fn, *args, timeout: float):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
    except asyncio.TimeoutError:
        raise ExternalAuthError(
            f"The directory did not answer within {int(timeout)} seconds (timeout, not a credentials problem)")


@router.post("/ldap/test")
async def test_ldap(
    data: TestCredentialsIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Test the LDAP settings (saved, or the unsaved form values when the
    request carries them). Without credentials, only the service bind is
    verified; with credentials, a full user authentication is performed,
    the matched mapping rule is shown and ZenPlus admission is evaluated
    separately from the directory result."""
    cfg = _with_override(await _load(db, LDAP_SETTINGS_KEY, LDAP_DEFAULTS), data.ldap, "bind_password")
    steps: list[dict] = []
    try:
        if not data.username:
            if not cfg.get("server") or not cfg.get("base_dn"):
                raise ExternalAuthError("LDAP server and base DN are required")
            await _run_blocking(ldap_test_bind, cfg, timeout=30)
            result = {"success": True, "message": "Connection and service bind OK",
                      "steps": [{"name": "connection", "ok": True}]}
            await _remember_test(db, "ldap", current_user, True, result["message"], cfg.get("server", ""))
            return result

        steps.append({"name": "connection", "ok": True})
        info = await _run_blocking(ldap_authenticate, cfg, data.username, data.password, timeout=40)
        if info is None:
            steps.append({"name": "credentials", "ok": False, "message": "User not found or invalid credentials"})
            return {"success": False, "message": "User not found or invalid credentials", "steps": steps}
        steps.append({"name": "credentials", "ok": True})
        role, matched = explain_ldap_role(cfg, info["groups"])
        steps.append({"name": "mapping", "ok": role is not None})
        roles = await _existing_roles(db)
        admission = await _admission(db, "ldap", cfg, data.username, role, roles)
        steps.append({"name": "admission", "ok": admission["would_sign_in"]})
        await _remember_test(db, "ldap", current_user, True, "User authentication OK", cfg.get("server", ""))
        return {
            "success": True,
            "message": f"Authenticated as {info['dn']}",
            "dn": info["dn"],
            "email": info["email"],
            "full_name": info["full_name"],
            "groups": info["groups"],
            "mapped_role": role,
            "matched_rule": matched,
            "steps": steps,
            **admission,
        }
    except ExternalAuthError as exc:
        await _remember_test(db, "ldap", current_user, False, str(exc), cfg.get("server", ""))
        return {"success": False, "message": str(exc), "steps": steps}
    except Exception as exc:  # ldap3 socket/TLS errors surface as a readable failure, not a 500
        msg = f"Cannot reach LDAP server {cfg.get('server')}:{cfg.get('port')}: {exc}"
        await _remember_test(db, "ldap", current_user, False, msg, cfg.get("server", ""))
        return {"success": False, "message": msg, "steps": steps}


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
    budget = min(int(cfg.get("timeout") or 5) * int(cfg.get("retries") or 3), RADIUS_MAX_TOTAL_SECONDS) + 5
    try:
        info = await _run_blocking(radius_authenticate, cfg, data.username, data.password, timeout=budget)
    except ExternalAuthError as exc:
        await _remember_test(db, "radius", current_user, False, str(exc), cfg.get("server", ""))
        return {"success": False, "message": str(exc), "steps": [{"name": "connection", "ok": False}]}
    if info is None:
        return {"success": False, "message": "Access-Reject: invalid credentials",
                "steps": [{"name": "connection", "ok": True}, {"name": "credentials", "ok": False}]}
    role, matched = explain_radius_role(cfg, info["reply_values"], info.get("reply_attributes"))
    roles = await _existing_roles(db)
    admission = await _admission(db, "radius", cfg, data.username, role, roles)
    await _remember_test(db, "radius", current_user, True, "Access-Accept", cfg.get("server", ""))
    return {
        "success": True,
        "message": "Access-Accept",
        "reply_values": info["reply_values"],
        "reply_attributes": [{"attribute": a, "value": v} for a, v in info.get("reply_attributes", [])],
        "mapped_role": role,
        "matched_rule": matched,
        "steps": [{"name": "connection", "ok": True}, {"name": "credentials", "ok": True},
                  {"name": "mapping", "ok": role is not None}, {"name": "admission", "ok": admission["would_sign_in"]}],
        **admission,
    }


@router.post("/oidc/test")
async def test_oidc(
    data: TestCredentialsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Check the issuer's discovery document and signing keys with the dialog's
    current (possibly unsaved) values, and show the redirect URI to register at
    the provider. It cannot complete a sign-in (that needs a browser), so it
    reports the provider checks only."""
    cfg = _with_override(await _load(db, oidc_service.OIDC_SETTINGS_KEY, oidc_service.OIDC_DEFAULTS), data.oidc, "client_secret")
    forwarded = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    redirect = oidc_service.redirect_uri(cfg, f"{forwarded.split(',')[0]}://{host.split(',')[0]}")
    steps: list[dict] = []
    try:
        oidc_service.clear_caches()
        doc = await oidc_service.discover(cfg)
        steps.append({"name": "connection", "ok": True})
        keys = (await oidc_service._jwks(cfg, doc, force=True)).get("keys", [])
        usable = [k for k in keys if k.get("kty") in ("RSA", "EC")]
        if not usable:
            raise oidc_service.OidcError("The provider publishes no usable signing keys")
        steps.append({"name": "credentials", "ok": True, "message": f"{len(usable)} signing key(s)"})
        extra = []
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            extra.append("Client ID and secret are not set yet")
        advertised = doc.get("id_token_signing_alg_values_supported") or []
        if advertised and not any(a in oidc_service.ALLOWED_ALGORITHMS for a in advertised):
            extra.append("The provider only offers signing algorithms ZenPlus does not accept")
        if cfg.get("groups_claim") and doc.get("claims_supported") and cfg["groups_claim"] not in doc["claims_supported"]:
            extra.append(f"The provider does not advertise a '{cfg['groups_claim']}' claim; group mapping may not work")
        result = {"success": True, "message": f"Discovery OK for {doc['issuer']}", "redirect_uri": redirect,
                  "authorization_endpoint": doc["authorization_endpoint"], "warnings": extra, "steps": steps}
        await _remember_test(db, "oidc", current_user, True, result["message"], cfg.get("issuer", ""))
        return result
    except oidc_service.OidcError as exc:
        await _remember_test(db, "oidc", current_user, False, str(exc), cfg.get("issuer", ""))
        return {"success": False, "message": str(exc), "redirect_uri": redirect, "steps": steps}
