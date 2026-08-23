"""Whether an alert's trigger notification actually went out.

A reset ("all clear") only means something to someone who was told the thing
broke. Quiet hours gate the trigger but deliberately never gated the recovery,
so a breach starting at 02:00 under a business-hours schedule produced a lone
"resolved" mail at 09:00 about an incident nobody was ever paged for. Worse,
nothing re-sent the trigger when the window opened: the condition sat breaching
all morning in silence, because an alert row already existed and the evaluators
only notify on the transition that creates one.

Both follow from the same missing fact — did the trigger actually go out? — so
it is recorded on the alert row as ``metadata->>'notified'``:

    "true"   trigger dispatched; a later recovery notice is warranted
    "false"  trigger suppressed; suppress the recovery too, and send the
             trigger late if the condition still breaches once the window opens
    absent   raised before this existed -> treated as notified, so upgrading
             never swallows the all-clear for an incident already in flight

Dispatch is recorded as attempted, not confirmed delivered: channel transports
swallow their own errors, so a bounced SMTP send still counts as notified. The
case this exists for is scheduled suppression, which is knowable up front.
"""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

async def stamp(db: AsyncSession, alert_id, sent: bool) -> None:
    """Record whether this alert's trigger notification was dispatched."""
    if alert_id is None:
        return
    await db.execute(
        text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
             "WHERE id = :id"),
        {"id": alert_id, "m": json.dumps({"notified": bool(sent)})},
    )


async def _flag(db: AsyncSession, alert_id) -> bool | None:
    if alert_id is None:
        return None
    row = (await db.execute(
        text("SELECT metadata->>'notified' FROM alerts WHERE id = :id"),
        {"id": alert_id},
    )).first()
    if not row or row[0] is None:
        return None
    return str(row[0]).lower() == "true"


async def was_notified(db: AsyncSession, alert_id) -> bool:
    """True when a recovery notice is warranted. Missing flag = legacy = yes."""
    flag = await _flag(db, alert_id)
    return True if flag is None else flag


async def is_pending(db: AsyncSession, alert_id) -> bool:
    """True only when the trigger was explicitly suppressed and still owes a send."""
    return (await _flag(db, alert_id)) is False


async def open_alert_id(db: AsyncSession, server_id: str, dedupe: str):
    """Id of the open host alert with this dedupe key, or None."""
    return (await db.execute(
        text("""SELECT id FROM alerts
                WHERE server_id = :sid AND status IN ('active','acknowledged')
                  AND metadata->>'dedupe' = :dedupe
                ORDER BY triggered_at DESC LIMIT 1"""),
        {"sid": server_id, "dedupe": dedupe},
    )).scalar()
