"""A discovery rule's own schedule (migrate-213, docs/discovery-rule-engine.md "Schedules").

A rule with the ``scheduled`` trigger either follows the shared engine
schedule (``schedule`` NULL, the phase 4 behaviour) or has its own:

* ``{"kind": "once", "at": "2026-12-01T22:00", "timezone": "Asia/Riyadh"}``
* ``{"kind": "hourly", "every_hours": 6, "time": "00:30", "timezone": ...}``:
  every N hours (N divides a day) from ``time`` each local day;
* ``{"kind": "weekly", "days": [0, 2, 4], "time": "02:00", "timezone": ...}``:
  on those weekdays (0 = Monday; all seven = daily);
* ``{"kind": "monthly", "day": 1, "time": "03:00", "timezone": ...}``: on that
  day of every month (1-28, so every month has it).

Times are local wall-clock times in the rule's IANA time zone, so a daily run
stays at 02:00 across a DST change. ``last_slot`` is the most recent slot at
or before ``now`` (None when there is none yet: a future one-time run).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

KINDS = ("once", "hourly", "weekly", "monthly")
HOURLY = (1, 2, 3, 4, 6, 8, 12)
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ScheduleError(ValueError):
    pass


def _tz(name: Any) -> str:
    name = str(name or "UTC")
    try:
        ZoneInfo(name)
    except Exception:
        raise ScheduleError(f"Unknown time zone: {name}") from None
    return name


def _hhmm(value: Any) -> str:
    v = str(value or "")
    if not _TIME.match(v):
        raise ScheduleError("Time: HH:MM, 24-hour (02:00)")
    return v


def validate(raw: Any, *, now: datetime | None = None) -> dict | None:
    """Normalised schedule, None for the shared schedule, or ``ScheduleError``.
    A one-time run must be in the future when it is saved."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScheduleError("A schedule is an object with a kind")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ScheduleError("Schedule: once, hourly, weekly or monthly")
    tz = _tz(raw.get("timezone"))
    if kind == "once":
        at = str(raw.get("at") or "")[:16]
        if not _AT.match(at):
            raise ScheduleError("Run once at: a date and time (YYYY-MM-DDTHH:MM)")
        try:
            when = datetime.fromisoformat(at).replace(tzinfo=ZoneInfo(tz))
        except ValueError:
            raise ScheduleError("Run once at: not a valid date") from None
        if when <= (now or datetime.now(timezone.utc)):
            raise ScheduleError("Run once at: choose a time in the future")
        return {"kind": "once", "at": at, "timezone": tz}
    out: dict[str, Any] = {"kind": kind, "time": _hhmm(raw.get("time") or "02:00"), "timezone": tz}
    if kind == "hourly":
        try:
            every = int(raw.get("every_hours"))
        except (TypeError, ValueError):
            every = 0
        if every not in HOURLY:
            raise ScheduleError("Every: 1, 2, 3, 4, 6, 8 or 12 hours")
        out["every_hours"] = every
    elif kind == "weekly":
        try:
            days = sorted({int(d) for d in raw.get("days") or []})
        except (TypeError, ValueError):
            days = []
        if not days or any(d < 0 or d > 6 for d in days):
            raise ScheduleError("Weekly: choose at least one day")
        out["days"] = days
    else:
        try:
            day = int(raw.get("day"))
        except (TypeError, ValueError):
            day = 0
        if not 1 <= day <= 28:
            raise ScheduleError("Monthly: a day from 1 to 28")
        out["day"] = day
    return out


def _at(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=tz)


def _month_back(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _month_fwd(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def last_slot(now: datetime, schedule: dict) -> datetime | None:
    """The most recent slot at or before ``now`` (aware UTC), or None."""
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    kind = schedule["kind"]
    if kind == "once":
        at = datetime.fromisoformat(schedule["at"]).replace(tzinfo=tz).astimezone(timezone.utc)
        return at if at <= now else None
    local = now.astimezone(tz)
    if kind == "hourly":
        from .reconcile import last_slot as shared_last
        return shared_last(now, {"schedule_timezone": schedule.get("timezone"), "schedule_time": schedule["time"],
                                 "schedule_every_hours": schedule["every_hours"]})
    if kind == "weekly":
        for back in range(8):
            d = local.date() - timedelta(days=back)
            if d.weekday() in schedule["days"]:
                slot = _at(d, schedule["time"], tz)
                if slot <= local:
                    return slot.astimezone(timezone.utc)
        return None
    d = local.date().replace(day=schedule["day"])
    for _ in range(3):
        slot = _at(d, schedule["time"], tz)
        if slot <= local:
            return slot.astimezone(timezone.utc)
        d = _month_back(d).replace(day=schedule["day"])
    return None


def next_slot(now: datetime, schedule: dict) -> datetime | None:
    """The first slot after ``now`` (aware UTC), or None (a one-time run that passed)."""
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    kind = schedule["kind"]
    if kind == "once":
        at = datetime.fromisoformat(schedule["at"]).replace(tzinfo=tz).astimezone(timezone.utc)
        return at if at > now else None
    local = now.astimezone(tz)
    if kind == "hourly":
        from .reconcile import next_slot as shared_next
        return shared_next(now, {"schedule_timezone": schedule.get("timezone"), "schedule_time": schedule["time"],
                                 "schedule_every_hours": schedule["every_hours"]})
    if kind == "weekly":
        for fwd in range(8):
            d = local.date() + timedelta(days=fwd)
            if d.weekday() in schedule["days"]:
                slot = _at(d, schedule["time"], tz)
                if slot > local:
                    return slot.astimezone(timezone.utc)
        return None
    d = local.date().replace(day=schedule["day"])
    for _ in range(3):
        slot = _at(d, schedule["time"], tz)
        if slot > local:
            return slot.astimezone(timezone.utc)
        d = _month_fwd(d).replace(day=schedule["day"])
    return None


def describe(schedule: dict | None) -> str:
    """Short wording for lists: "Daily at 02:00 (Asia/Riyadh)"."""
    if not schedule:
        return "Shared schedule"
    tz = schedule.get("timezone") or "UTC"
    kind = schedule["kind"]
    if kind == "once":
        return f"Once on {schedule['at'].replace('T', ' at ')} ({tz})"
    if kind == "hourly":
        n = schedule["every_hours"]
        return f"Every {'hour' if n == 1 else f'{n} hours'} from {schedule['time']} ({tz})"
    if kind == "weekly":
        days = schedule["days"]
        when = "Daily" if len(days) == 7 else ("Weekdays" if days == [0, 1, 2, 3, 4] else
                                               ", ".join(DAY_NAMES[d] for d in days))
        return f"{when} at {schedule['time']} ({tz})"
    return f"Monthly on day {schedule['day']} at {schedule['time']} ({tz})"


def starting_point(schedule: dict | None, now: datetime) -> datetime | None:
    """``schedule_last_slot`` for a schedule saved (or a rule activated) at
    ``now``: the current slot, so the next one runs and a missed one is not
    caught up. None for a one-time run (it runs when its time comes)."""
    if not schedule or schedule["kind"] == "once":
        return None
    return last_slot(now, schedule)


def due(schedule: dict, last_done: datetime | None, now: datetime) -> datetime | None:
    """The slot to run now, or None. A recurring rule with no starting point
    yet only gets one (see ``starting_point``); it is not run."""
    slot = last_slot(now, schedule)
    if slot is None:
        return None
    if last_done is None:
        return slot if schedule["kind"] == "once" else None
    return slot if slot > last_done else None
