"""Built-in role maintenance (RP-12).

The four built-in roles are defined in code (``BUILTIN_ROLE_DEFINITIONS``) and
persisted in ``roles``. When the code's definition version is newer than a
row's ``definition_version`` the row is reconciled - once, under an advisory
lock, with a history entry showing exactly which permissions were added or
removed. Custom roles are never touched, so a permission introduced in a later
release is never granted to a custom role implicitly.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    BUILTIN_ROLE_DEFINITION_VERSION,
    BUILTIN_ROLE_DEFINITIONS,
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
                "SELECT id, permissions, COALESCE(definition_version, 0) FROM roles "
                "WHERE name = :n AND is_system"), {"n": name})).first()
            if row is None or row[2] >= BUILTIN_ROLE_DEFINITION_VERSION:
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
