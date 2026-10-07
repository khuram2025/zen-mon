"""Built-in role maintenance (RP-12).

The built-in roles are defined in code (``BUILTIN_ROLE_DEFINITIONS``) and
persisted in ``roles``. When the code's definition version is newer than a
row's ``definition_version`` the row is reconciled - once, under an advisory
lock, with a history entry showing exactly which permissions were added or
removed. A built-in introduced by a later release (the team roles) is created
the same way. Custom roles are never touched, so a permission introduced in a
later release is never granted to a custom role implicitly; a custom role that
already uses a new built-in's name keeps it, and that built-in is skipped.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    BUILTIN_ROLE_DEFINITION_VERSION,
    BUILTIN_ROLE_DEFINITIONS,
    BUILTIN_ROLE_META,
)
from app.core.security import invalidate_role_cache

logger = logging.getLogger(__name__)

_LOCK_KEY = 0x5A50_524F_4C45  # "ZPROLE"


async def reconcile_builtin_roles(db: AsyncSession) -> list[dict]:
    """Bring built-in role rows to the code definitions. Returns what changed."""
    changed: list[dict] = []
    try:
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        for name, perms in BUILTIN_ROLE_DEFINITIONS.items():
            row = (await db.execute(text(
                "SELECT id, permissions, COALESCE(definition_version, 0), is_system FROM roles "
                "WHERE name = :n"), {"n": name})).first()
            if row is None and name in BUILTIN_ROLE_META:
                if await _create_builtin(db, name, perms):
                    changed.append({"role": name, "added": sorted(set(perms)), "removed": [], "created": True})
                continue
            if row is None or not row[3] or row[2] >= BUILTIN_ROLE_DEFINITION_VERSION:
                continue
            before = sorted(row[1] if isinstance(row[1], list) else [])
            after = sorted(set(perms))
            await db.execute(text(
                "UPDATE roles SET permissions = CAST(:p AS jsonb), definition_version = :v, "
                "revision = revision + 1, updated_at = NOW(), updated_by = 'system' WHERE id = :id"),
                {"p": json.dumps(after), "v": BUILTIN_ROLE_DEFINITION_VERSION, "id": row[0]})
            await db.execute(text(
                "INSERT INTO role_history (role_id, role_name, revision, action, actor_username, added, removed, "
                "permissions_after, affected_users, reason, changes) "
                "SELECT r.id, r.name, r.revision, 'builtin_reconcile', 'system', CAST(:added AS jsonb), "
                "CAST(:removed AS jsonb), CAST(:after AS jsonb), "
                "(SELECT count(*) FROM users u WHERE u.role = r.name), :reason, CAST(:changes AS jsonb) "
                "FROM roles r WHERE r.id = :id"),
                {"added": json.dumps(sorted(set(after) - set(before))),
                 "removed": json.dumps(sorted(set(before) - set(after))),
                 "after": json.dumps(after), "id": row[0],
                 "reason": f"Built-in role definition version {BUILTIN_ROLE_DEFINITION_VERSION}",
                 "changes": json.dumps({"definition_version": BUILTIN_ROLE_DEFINITION_VERSION})})
            changed.append({"role": name, "added": sorted(set(after) - set(before)),
                            "removed": sorted(set(before) - set(after))})
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("built-in role reconciliation failed (continuing with persisted roles)")
        return []
    if changed:
        invalidate_role_cache()
        logger.info("built-in roles reconciled: %s", changed)
    return changed


async def _create_builtin(db: AsyncSession, name: str, perms: list[str]) -> bool:
    """Insert a built-in role introduced after the appliance was installed."""
    display_name, description = BUILTIN_ROLE_META[name]
    after = sorted(set(perms))
    row = (await db.execute(text(
        "INSERT INTO roles (name, display_name, description, permissions, is_system, revision, "
        "definition_version, updated_by) VALUES (:n, :d, :desc, CAST(:p AS jsonb), TRUE, 1, :v, 'system') "
        "ON CONFLICT (name) DO NOTHING RETURNING id"),
        {"n": name, "d": display_name, "desc": description, "p": json.dumps(after),
         "v": BUILTIN_ROLE_DEFINITION_VERSION})).first()
    if row is None:
        return False
    await db.execute(text(
        "INSERT INTO role_history (role_id, role_name, revision, action, actor_username, added, removed, "
        "permissions_after, affected_users, reason, changes) VALUES (:id, :n, 1, 'builtin_create', 'system', "
        "CAST(:after AS jsonb), '[]'::jsonb, CAST(:after AS jsonb), 0, :reason, CAST(:changes AS jsonb))"),
        {"id": row[0], "n": name, "after": json.dumps(after),
         "reason": f"Built-in role definition version {BUILTIN_ROLE_DEFINITION_VERSION}",
         "changes": json.dumps({"definition_version": BUILTIN_ROLE_DEFINITION_VERSION,
                                "display_name": display_name})})
    return True
