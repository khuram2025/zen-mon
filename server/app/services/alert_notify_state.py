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

    "true"     trigger dispatched; a later recovery notice is warranted
    "false"    trigger suppressed; suppress the recovery too, and send the
               trigger late if the condition still breaches once the window opens
    "sending"  a worker owns the trigger send right now: neither pending (no
               other worker or pass may send it again) nor notified (no
               recovery or escalation for a trigger that may not have gone
               out). Evaluators insert new rows in this state and stamp the
               outcome after the send; a late send claims the row with
               :func:`claim_pending` (false -> sending) before it dispatches.
               A claim older than ``STALE_SENDING_S`` means the worker died
               mid-send: it becomes pending again, so a crash cannot lose
               the page for good (a live send finishes in seconds).
    absent     raised before this existed -> treated as notified, so upgrading
               never swallows the all-clear for an incident already in flight

Dispatch is recorded as attempted, not confirmed delivered: channel transports
swallow their own errors, so a bounced SMTP send still counts as notified. The
case this exists for is scheduled suppression, which is knowable up front.
"""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SENDING = "sending"
# A send claimed longer ago than this was interrupted (worker crash, restart).
STALE_SENDING_S = 600


def sending_metadata() -> dict:
    """Metadata that marks a trigger send as in flight, with its claim time."""
    from datetime import datetime, timezone
    return {"notified": SENDING, "sending_since": datetime.now(timezone.utc).isoformat()}


_STALE_SENDING_SQL = (
    "(metadata->>'notified' = 'sending' AND"
    " (CASE WHEN metadata->>'sending_since' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'"
    " THEN (metadata->>'sending_since')::timestamptz ELSE triggered_at END)"
    f" < now() - make_interval(secs => {STALE_SENDING_S}))"
)


async def stamp(db: AsyncSession, alert_id, sent: bool) -> None:
    """Record whether this alert's trigger notification was dispatched."""
    if alert_id is None:
        return
    await db.execute(
        text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
             "WHERE id = :id"),
        {"id": alert_id, "m": json.dumps({"notified": bool(sent)})},
    )


async def _flag(db: AsyncSession, alert_id) -> str | None:
    """The stored flag, lower-cased ("true", "false", "sending"), or None."""
    if alert_id is None:
        return None
    row = (await db.execute(
        text("SELECT metadata->>'notified' FROM alerts WHERE id = :id"),
        {"id": alert_id},
    )).first()
    if not row or row[0] is None:
        return None
    return str(row[0]).lower()


def flag_means_notified(flag) -> bool:
    """Whether a raw ``metadata.notified`` value says the trigger went out.

    Absent = legacy = yes; only an explicit "true" otherwise. "sending" is a
    send in flight (or interrupted) and is not evidence of a delivery.
    """
    if flag is None:
        return True
    return str(flag).lower() == "true"


async def was_notified(db: AsyncSession, alert_id) -> bool:
    """True when a recovery notice is warranted. Missing flag = legacy = yes."""
    return flag_means_notified(await _flag(db, alert_id))


async def is_pending(db: AsyncSession, alert_id) -> bool:
    """True when the trigger still owes a send: explicitly suppressed, or a
    send that was claimed but interrupted (stale "sending")."""
    if alert_id is None:
        return False
    row = (await db.execute(
        text("SELECT metadata->>'notified', metadata->>'sending_since', triggered_at "
             "FROM alerts WHERE id = :id"),
        {"id": alert_id},
    )).first()
    if not row or row[0] is None:
        return False
    flag = str(row[0]).lower()
    if flag == "false":
        return True
    if flag != SENDING:
        return False
    since = _parse_ts(row[1] if len(row) > 1 else None) or (row[2] if len(row) > 2 else None)
    if since is None:
        return False
    from datetime import datetime, timezone
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - since).total_seconds() > STALE_SENDING_S


def _parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def claim_pending(db: AsyncSession, alert_id) -> bool:
    """Take ownership of a suppressed trigger's late send (false -> sending).

    A conditional UPDATE, so of two workers (or two passes) that both saw the
    trigger as pending exactly one wins. The caller commits before sending
    and records the outcome with :func:`stamp` afterwards.
    """
    if alert_id is None:
        return False
    row = (await db.execute(
        text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
             "WHERE id = :id AND status = 'active' AND (metadata->>'notified' = 'false' OR "
             + _STALE_SENDING_SQL + ") RETURNING id"),
        {"id": alert_id, "m": json.dumps(sending_metadata())},
    )).first()
    return row is not None


async def open_alert_id(db: AsyncSession, server_id: str, dedupe: str):
    """Id of the open host alert with this dedupe key, or None."""
    return (await db.execute(
        text("""SELECT id FROM alerts
                WHERE server_id = :sid AND status IN ('active','acknowledged')
                  AND metadata->>'dedupe' = :dedupe
                ORDER BY triggered_at DESC LIMIT 1"""),
        {"sid": server_id, "dedupe": dedupe},
    )).scalar()
