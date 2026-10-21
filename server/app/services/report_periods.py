"""Report periods: rolling windows and calendar windows in the organisation
timezone.

The server-side twin of ``dashboard/src/features/reports/periods.ts`` (same
ids, labels and window rules), so a schedule delivers exactly the window the
viewer shows for the same period:

* rolling ``last_24h`` / ``last_7d`` / ``last_30d`` / ``last_90d`` end now;
* calendar windows start at local midnight in the organisation timezone.
  ``today`` / ``week_to_date`` / ``month_to_date`` / ``quarter_to_date`` end
  now; ``yesterday`` / ``previous_week`` / ``previous_month`` /
  ``previous_quarter`` end at the local midnight that closes them (exclusive).

Local midnights are resolved with zoneinfo, so windows that span a daylight
saving change are 23 or 25 hours long per affected day, as on a wall clock.
Weeks start on Monday, Sunday or Saturday (Settings -> General -> Company).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# (id, label, kind) in the order the dashboard offers them.
PERIODS: tuple[tuple[str, str, str], ...] = (
    ("last_24h", "Last 24 hours", "rolling"),
    ("last_7d", "Last 7 days", "rolling"),
    ("last_30d", "Last 30 days", "rolling"),
    ("last_90d", "Last 90 days", "rolling"),
    ("today", "Today", "calendar"),
    ("yesterday", "Yesterday", "calendar"),
    ("week_to_date", "This week", "calendar"),
    ("previous_week", "Previous week", "calendar"),
    ("previous_work_week", "Previous work week", "calendar"),
    ("month_to_date", "This month", "calendar"),
    ("previous_month", "Previous month", "calendar"),
    ("quarter_to_date", "This quarter", "calendar"),
    ("previous_quarter", "Previous quarter", "calendar"),
)
PERIOD_IDS: tuple[str, ...] = tuple(p[0] for p in PERIODS)
PERIOD_LABELS: dict[str, str] = {p[0]: p[1] for p in PERIODS}
ROLLING_HOURS: dict[str, int] = {"last_24h": 24, "last_7d": 7 * 24, "last_30d": 30 * 24, "last_90d": 90 * 24}
# Windows that close at a local midnight (the rest end "now").
CLOSED_PERIODS = frozenset({"yesterday", "previous_week", "previous_work_week", "previous_month",
                            "previous_quarter"})
# A work week is the first WORK_WEEK_DAYS days of the organisation week:
# Sunday -> Sun-Thu, Monday -> Mon-Fri, Saturday -> Sat-Wed.
WORK_WEEK_DAYS = 5

# JavaScript Date.getDay() numbering (Sunday = 0), as periods.ts uses.
WEEK_START_DOW: dict[str, int] = {"sunday": 0, "monday": 1, "saturday": 6}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def is_period(value: object) -> bool:
    return isinstance(value, str) and value in PERIOD_LABELS


def is_calendar(period: str) -> bool:
    return is_period(period) and period not in ROLLING_HOURS


def _zone(tz: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def _utc(dt: datetime) -> datetime:
    """Aware UTC; naive input is taken as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_midnight(day: date, tz: Optional[str]) -> datetime:
    """The UTC instant at which ``day`` begins in ``tz``.

    zoneinfo resolves a midnight that falls in a spring-forward gap (zones
    that change clocks at 00:00) to the first instant of that local day, and
    an ambiguous midnight to its first occurrence."""
    return datetime(day.year, day.month, day.day, tzinfo=_zone(tz)).astimezone(timezone.utc)


def _fmt_day(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def window_label(period: str, start: datetime, end: datetime, tz: Optional[str]) -> str:
    """'Previous month (1 Sep 2026 – 30 Sep 2026)': the period name and the
    local calendar days it covers (a single day is named once)."""
    zone = _zone(tz)
    first = _utc(start).astimezone(zone).date()
    last_instant = _utc(end) - timedelta(microseconds=1) if period in CLOSED_PERIODS else _utc(end)
    last = max(first, last_instant.astimezone(zone).date())
    name = PERIOD_LABELS.get(period, period)
    days = _fmt_day(first) if first == last else f"{_fmt_day(first)} – {_fmt_day(last)}"
    return f"{name} ({days})"


def period_range(period: str, tz: Optional[str], week_start: str = "monday",
                 now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """[start, end) for ``period`` as aware UTC datetimes (see module doc).
    Raises ValueError for an unknown period id."""
    now_utc = _utc(now) if now is not None else datetime.now(timezone.utc)
    if period in ROLLING_HOURS:
        return now_utc - timedelta(hours=ROLLING_HOURS[period]), now_utc
    if period not in PERIOD_LABELS:
        raise ValueError(f"unknown report period {period!r}")

    zone = _zone(tz)
    local = now_utc.astimezone(zone)
    today = local.date()

    def start_of(d: date) -> datetime:
        return local_midnight(d, tz)

    if period == "today":
        return start_of(today), now_utc
    if period == "yesterday":
        return start_of(today - timedelta(days=1)), start_of(today)
    if period in ("week_to_date", "previous_week", "previous_work_week"):
        dow = (today.weekday() + 1) % 7  # Sunday = 0
        first_dow = WEEK_START_DOW.get((week_start or "monday").strip().lower(), 1)
        week_first = today - timedelta(days=(dow - first_dow + 7) % 7)
        if period == "week_to_date":
            return start_of(week_first), now_utc
        if period == "previous_work_week":
            # The latest work week that has ended: this week's once its last
            # work day is over, else last week's.
            if today < week_first + timedelta(days=WORK_WEEK_DAYS):
                week_first -= timedelta(days=7)
            return start_of(week_first), start_of(week_first + timedelta(days=WORK_WEEK_DAYS))
        return start_of(week_first - timedelta(days=7)), start_of(week_first)
    if period == "month_to_date":
        return start_of(today.replace(day=1)), now_utc
    if period == "previous_month":
        this_month = today.replace(day=1)
        prev = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
        return start_of(prev), start_of(this_month)
    # quarter_to_date / previous_quarter
    q_month = (today.month - 1) // 3 * 3 + 1
    q_first = date(today.year, q_month, 1)
    if period == "quarter_to_date":
        return start_of(q_first), now_utc
    prev_q = date(today.year - 1, 10, 1) if q_month == 1 else date(today.year, q_month - 3, 1)
    return start_of(prev_q), start_of(q_first)


def resolve_period(period: str, tz: str, week_start: str = "monday",
                   now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """(start, end, label) for a report period in the organisation timezone:
    aware UTC ``start`` (inclusive) and ``end`` (exclusive), and a label such
    as 'Previous month (1 Sep 2026 – 30 Sep 2026)'. An unknown timezone is
    UTC, an unknown week start is Monday; an unknown period raises ValueError."""
    start, end = period_range(period, tz, week_start, now)
    return start, end, window_label(period, start, end, tz)
