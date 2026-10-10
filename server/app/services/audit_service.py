from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class AuditUnavailable(RuntimeError):
    """The audit entry of an action that must not happen unaudited could not be stored."""


async def write_audit_log(
    db: AsyncSession,
    *,
    actor: User | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    required: bool = False,
) -> bool:
    """Audit insert; best-effort unless ``required``.

    The nested transaction keeps audit failures isolated from the primary
    action. This matters during rolling upgrades where the audit table may not
    exist until migrations have run.

    By default a failed insert is logged and swallowed, and the function returns False. An action that promises
    its audit entry before it takes effect (a data export, a settings change) passes ``required=True``: the
    failure is then raised as ``AuditUnavailable`` (after the savepoint was rolled back, so the caller's
    transaction is intact and the caller decides whether to roll the rest back) and the caller refuses the action.

    ``actor_id`` is resolved through ``users`` so a user row that vanished since the request authenticated can
    never fail the insert on its foreign key (``ON DELETE SET NULL`` already treats the entry as outliving the user).
    Returns True when the entry was stored.
    """
    try:
        async with db.begin_nested():
            await db.execute(
                text(
                    """
                    INSERT INTO audit_logs (
                        actor_id, actor_username, actor_role,
                        action, resource_type, resource_id, metadata
                    ) VALUES (
                        (SELECT id FROM users WHERE id = :actor_id), :actor_username, :actor_role,
                        :action, :resource_type, :resource_id, CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "actor_id": getattr(actor, "id", None),
                    "actor_username": getattr(actor, "username", None),
                    "actor_role": getattr(actor, "role", None),
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "metadata": json.dumps(metadata or {}, default=str),
                },
            )
    except Exception as exc:
        # Audit must never block the requested admin operation, unless the operation said it must not go unaudited.
        logging.getLogger(__name__).exception("Could not persist audit event %s", action)
        if required:
            raise AuditUnavailable(f"audit event {action} could not be stored") from exc
        return False
    return True
