"""Event alert expiry policy.

* ``GET/PUT /alerts/event-expiry`` — ``system_settings['alert_event_expiry']``
  (on by default, 168 hours) with a plain-language summary and the number of
  open event alerts the current window would close now.

Reads need ``alerts.view``, changes ``alerts.manage``; a change is audited.
Mounted before the Alert Center router so ``/alerts/event-expiry`` is never
read as an alert id. Logic: app.services.alert_expiry.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import alert_expiry as expiry
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])

can_view = require_permission("alerts.view")
can_manage = require_permission("alerts.manage")


class EventExpiryPolicy(BaseModel):
    enabled: bool = True
    hours: int = Field(default=168, ge=expiry.HOURS_LIMITS[0], le=expiry.HOURS_LIMITS[1])


async def _due(db: AsyncSession, settings: dict) -> int | None:
    """Open event alerts the window would close on the next pass."""
    try:
        async with db.begin_nested():
            return int((await db.execute(text(
                f"SELECT count(*) FROM alerts a WHERE {expiry.expirable_sql('a')}"
            ), {"hours": int(settings["hours"])})).scalar() or 0)
    except Exception:
        return None


async def _payload(db: AsyncSession, settings: dict) -> dict:
    return {**settings, "summary": expiry.policy_sentence(settings),
            "due_now": await _due(db, settings),
            "limits": {"hours": list(expiry.HOURS_LIMITS)},
            "defaults": dict(expiry.DEFAULT_SETTINGS)}


@router.get("/event-expiry")
async def get_policy(db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    return await _payload(db, await expiry.load_settings(db))


@router.put("/event-expiry")
async def put_policy(body: EventExpiryPolicy, db: AsyncSession = Depends(get_db),
                     user: User = Depends(can_manage)):
    before = await expiry.load_settings(db)
    after = await expiry.save_settings(db, body.model_dump(), actor_id=getattr(user, "id", None))
    await write_audit_log(db, actor=user, action="alert.event_expiry.update", resource_type="settings",
                          resource_id=expiry.SETTINGS_KEY, metadata={"before": before, "after": after})
    await db.commit()
    return await _payload(db, after)
