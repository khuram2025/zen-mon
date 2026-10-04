"""One definition of when a scheduled NCCM backup is due.

The scheduler, the bulk-settings preview and the device page all use
`next_due`, so a preview always matches what the timer will actually do.
Daily/weekly times are wall-clock times in the device's IANA timezone
(zoneinfo handles DST). A last-run stamp in the future means the appliance
clock was corrected backwards; it is treated as unknown rather than letting
the schedule stall until that future moment.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLOCK_TOLERANCE = timedelta(minutes=5)


def _zone(name):
    try:
        return ZoneInfo(name or 'UTC')
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo('UTC')


def _time(value):
    if isinstance(value, dtime):
        return value
    if isinstance(value, str) and value:
        hh, mm = value.split(':')[:2]
        return dtime(int(hh), int(mm))
    return None


def _occurrence(day, at, tz):
    return datetime.combine(day, at).replace(tzinfo=tz).astimezone(timezone.utc)


def _dow(day):
    # Postgres DOW convention used by the stored schedule: 0=Sunday .. 6=Saturday.
    return (day.weekday() + 1) % 7


def clock_anomaly(last, now):
    return bool(last and last > now + CLOCK_TOLERANCE)


def next_due(s, now=None):
    """Return the UTC datetime at which the schedule is (or was) next due, or
    None when scheduling is off or incompletely configured. A value <= now
    means the backup is due now."""
    now = now or datetime.now(timezone.utc)
    get = (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
    if not get('schedule_enabled'):
        return None
    last = get('last_scheduled_at')
    if isinstance(last, str):
        last = datetime.fromisoformat(last)
    if clock_anomaly(last, now):
        last = None
    kind = get('schedule_type') or 'interval'
    if kind == 'interval':
        hours = int(get('schedule_interval_hours') or 24)
        return now if last is None else last + timedelta(hours=hours)
    at = _time(get('schedule_time'))
    if at is None:
        return None
    days = set(get('schedule_days') or []) if kind == 'weekly' else set(range(7))
    if not days:
        return None
    tz = _zone(get('schedule_timezone'))
    local_today = now.astimezone(tz).date()
    # The most recent occurrence at or before now. With no previous run, only
    # an occurrence from today counts, so enabling a 02:00 schedule at 01:00
    # waits for 02:00 instead of firing immediately for yesterday's slot.
    for back in range(0, 8):
        day = local_today - timedelta(days=back)
        if _dow(day) in days:
            occ = _occurrence(day, at, tz)
            if occ <= now:
                if last is None:
                    if back == 0:
                        return occ
                    break
                if last < occ:
                    return occ
                break
    for ahead in range(0 if last is None else 0, 9):
        day = local_today + timedelta(days=ahead)
        if _dow(day) in days:
            occ = _occurrence(day, at, tz)
            if occ > now:
                return occ
    return None


_SCHEDULE_KEYS = ('schedule_enabled', 'schedule_type', 'schedule_interval_hours', 'schedule_time', 'schedule_days', 'schedule_timezone')


def _norm(v):
    if isinstance(v, dtime):
        return v.strftime('%H:%M')
    if isinstance(v, (list, tuple)):
        return sorted(v)
    return v


def reanchor(before, after, now=None):
    """When a daily/weekly schedule is enabled or changed, return `now` to use
    as the last-run anchor so it starts at its next slot instead of running
    immediately for a slot that passed before the change. Interval schedules
    keep their "run now, then every N hours" behaviour."""
    now = now or datetime.now(timezone.utc)
    if not after.get('schedule_enabled') or (after.get('schedule_type') or 'interval') == 'interval':
        return None
    changed = any(_norm(before.get(k)) != _norm(after.get(k)) for k in _SCHEDULE_KEYS)
    last = after.get('last_scheduled_at')
    if changed and (last is None or last < now):
        return now
    return None


def is_due(s, now=None):
    now = now or datetime.now(timezone.utc)
    due = next_due(s, now)
    return due is not None and due <= now


def describe(s):
    """Human summary of a schedule (e.g. 'Weekly Mon, Wed at 02:00 Asia/Riyadh')."""
    get = (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
    if not get('schedule_enabled'):
        return 'Not scheduled'
    kind = get('schedule_type') or 'interval'
    tz = get('schedule_timezone') or 'UTC'
    if kind == 'interval':
        return f"Every {get('schedule_interval_hours') or 24} h"
    at = _time(get('schedule_time'))
    when = at.strftime('%H:%M') if at else '—'
    if kind == 'daily':
        return f'Daily at {when} {tz}'
    names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    days = ', '.join(names[d] for d in sorted(get('schedule_days') or []))
    return f'Weekly {days or "(no days)"} at {when} {tz}'
