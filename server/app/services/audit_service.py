from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def write_audit_log(
    db: AsyncSession,
    *,
    actor: User | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit insert.

    The nested transaction keeps audit failures isolated from the primary
    action. This matters during rolling upgrades where the audit table may not
    exist until migrations have run.
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
                        :actor_id, :actor_username, :actor_role,
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
    except Exception:
        # Audit must never block the requested admin operation.
        logging.getLogger(__name__).exception("Could not persist audit event %s", action)
        return
