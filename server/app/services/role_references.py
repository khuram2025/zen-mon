"""Where a role is referenced outside ``users.role`` (RP-10): the LDAP /
RADIUS role mappings and their default roles, stored in ``system_settings``.

Reads the *raw* stored value (secrets stay encrypted) and never commits - the
callers do the rewrite inside their own transaction so a rename migrates every
reference atomically.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PROVIDERS = (
    ("auth.ldap", "LDAP / Active Directory", "group_mappings", "group"),
    ("auth.radius", "RADIUS", "class_mappings", "value"),
    ("auth.oidc", "OpenID Connect", "group_mappings", "group"),
)


async def _raw(db: AsyncSession, key: str) -> dict | None:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": key})).first()
    return row[0] if row and isinstance(row[0], dict) else None


async def provider_role_references(db: AsyncSession, role_name: str) -> list[dict]:
    refs: list[dict] = []
    for key, label, list_key, match_key in _PROVIDERS:
        cfg = await _raw(db, key)
        if not cfg:
            continue
        for m in cfg.get(list_key) or []:
            if isinstance(m, dict) and m.get("role") == role_name:
                refs.append({"provider": key.split(".")[1], "provider_label": label, "kind": "mapping",
                             "match": m.get(match_key) or "", "enabled": bool(cfg.get("enabled"))})
        if cfg.get("default_role") == role_name:
            refs.append({"provider": key.split(".")[1], "provider_label": label, "kind": "default_role",
                         "match": "", "enabled": bool(cfg.get("enabled"))})
    return refs


async def rewrite_provider_role_references(db: AsyncSession, old: str, new: str) -> int:
    """Point every mapping / default that names ``old`` at ``new``."""
    changed = 0
    for key, _label, list_key, _match in _PROVIDERS:
        cfg = await _raw(db, key)
        if not cfg:
            continue
        dirty = False
        for m in cfg.get(list_key) or []:
            if isinstance(m, dict) and m.get("role") == old:
                m["role"] = new
                dirty = True
                changed += 1
        if cfg.get("default_role") == old:
            cfg["default_role"] = new
            dirty = True
            changed += 1
        if dirty:
            await db.execute(
                text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                {"k": key, "v": json.dumps(cfg)},
            )
    return changed
