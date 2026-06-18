"""Alert-rule notification scheduling (quiet hours).

Alert rules may carry an optional active window: ``schedule_start`` /
``schedule_end`` (TIME) and ``schedule_days`` (list of ISO weekday ints,
1=Mon … 7=Sun). When set, notifications for that rule are only dispatched while
"now" falls inside the window — the alert is still recorded, only the outbound
notification is suppressed outside the window. An unset/empty schedule always
allows notifications, preserving prior behaviour.

Windows are evaluated in the appliance's configured timezone (the ``company``
system-setting's ``timezone``, default UTC), so "09:00–17:00 Mon–Fri" means
local business hours, not UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.alert_schedule")


async def get_configured_timezone(db: AsyncSession) -> str:
    """Return the appliance's configured IANA timezone name (default 'UTC')."""
    try:
        row = (await db.execute(
            text("SELECT value FROM system_settings WHERE key = 'company'")
        )).first()
        if row and isinstance(row[0], dict):
            return row[0].get("timezone") or "UTC"
    except Exception as exc:
        logger.debug("could not read configured timezone: %s", exc)
    return "UTC"


def _parse_time(v) -> time | None:
    if v is None or v == "":
        return None
    if isinstance(v, time):
        return v
    s = str(v)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _normalize_days(days) -> set[int]:
    """Coerce schedule_days into a set of ISO weekday ints (1=Mon..7=Sun)."""
    if not days:
        return set()
    out: set[int] = set()
    for d in days:
        try:
            n = int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 7:
            out.add(n)
    return out


def notifications_allowed(
    schedule_start,
    schedule_end,
    schedule_days,
    tz_name: str = "UTC",
    now: datetime | None = None,
) -> bool:
    """True if a notification may be sent right now under this rule's schedule.

    - No time window and no day restriction → always True.
    - Day restriction: today's ISO weekday must be in ``schedule_days`` (when a
      non-full set is given).
    - Time window: ``start <= now < end``; a window where start > end is treated
      as spanning midnight (e.g. 22:00–06:00).
    """
    start = _parse_time(schedule_start)
    end = _parse_time(schedule_end)
    days = _normalize_days(schedule_days)

    # Nothing configured ⇒ unrestricted.
    if start is None and end is None and (not days or len(days) == 7):
        return True

    try:
        tz = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, Exception):
        tz = timezone.utc
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)

    # Day-of-week gate.
    if days and len(days) < 7 and now.isoweekday() not in days:
        return False

    # Time-window gate (only when both bounds are set).
    if start is not None and end is not None:
        t = now.time()
        if start <= end:
            return start <= t < end
        # Overnight window (wraps past midnight).
        return t >= start or t < end

    return True
