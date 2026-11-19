"""Maintenance windows: when one is in force and which assets it covers.

``device_maintenance`` (migrate-053, recurrence and server scope migrate-207)
holds one-off and recurring windows. See ``docs/maintenance-windows.md``.

**When.** The database function ``device_maintenance_occurrences(m, from, to)``
is the one definition of when a window is in force; ``device_maintenance_active
(m, at)`` wraps it for "now". Every evaluator asks the database through the
helpers here (or the SQL fragments below), the Go poller and the sensor API
use the same functions, so they all agree. The pure functions in this module
(``occurrences``, ``active_at``, ``next_occurrence``, ``first_occurrence``)
mirror the SQL for the API (the next occurrence shown in the UI, the first
occurrence written to ``starts_at``/``ends_at``); a pg test checks that the two
agree, across DST changes too.

* One-off: in force while ``starts_at <= at <= ends_at`` (unchanged).
* Recurring: an occurrence starts on each local day whose ISO weekday is in
  ``recur_days``, at ``recur_start_time`` in ``recur_timezone``, and lasts
  ``recur_duration_minutes`` absolute minutes (so it may run past midnight and
  is never stretched or shortened by a DST change). Occurrences that start
  before ``starts_at`` (the first occurrence) or on a local day after
  ``recur_until`` do not count. A local start time that falls in a DST gap or
  overlap is read as PostgreSQL reads it: the later of the two instants.

**What.** ``device``/``group``/``tag``/``all`` windows cover devices (a group
window covers subgroups through ``device_group_member_closure``);
``server``/``server_group``/``server_tag`` windows cover servers. A server is
also in maintenance while the device it is linked to (``servers.device_id``)
is: they are the same machine.

What happens when a window ends does not depend on its kind: the poller (and
a sensor) applies the confirmed status with its alerts once the device leaves
the active set, and evaluators that skipped the asset judge it again on their
next pass.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.maintenance_windows")

UTC = timezone.utc
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 7 * 24 * 60

DEVICE_SCOPES = ("device", "group", "tag", "all")
SERVER_SCOPES = ("server", "server_group", "server_tag")
SCOPES = DEVICE_SCOPES + SERVER_SCOPES

DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# ─── Occurrence math (mirrors device_maintenance_occurrences) ───────────────

@dataclass(frozen=True)
class Recurrence:
    days: tuple[int, ...]
    start_time: time
    duration_minutes: int
    timezone: str
    until: Optional[date] = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def recurrence_of(row) -> Optional[Recurrence]:
    """The recurrence of a device_maintenance row (mapping or object), or None
    for a one-off window."""
    def get(key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    days = get("recur_days")
    if not days:
        return None
    return Recurrence(days=tuple(sorted({int(d) for d in days})), start_time=get("recur_start_time"),
                      duration_minutes=int(get("recur_duration_minutes")),
                      timezone=str(get("recur_timezone")), until=get("recur_until"))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_start(day: date, start: time, tz: ZoneInfo) -> datetime:
    """``day`` at ``start`` in ``tz``, as UTC. In a DST gap or overlap both
    readings exist (fold 0 and 1); PostgreSQL takes the later instant (the
    offset before a spring-forward change, after a fall-back change)."""
    naive = datetime.combine(day, start.replace(tzinfo=None))
    a = naive.replace(tzinfo=tz).astimezone(UTC)
    b = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    return max(a, b)


def occurrences(starts_at: datetime, ends_at: datetime, rec: Optional[Recurrence],
                frm: datetime, to: datetime) -> list[tuple[datetime, datetime]]:
    """The occurrences overlapping [frm, to] (inclusive), oldest first."""
    starts_at, ends_at, frm, to = _aware(starts_at), _aware(ends_at), _aware(frm), _aware(to)
    if rec is None:
        return [(starts_at, ends_at)] if starts_at <= to and ends_at >= frm else []
    tz = rec.tz
    length = timedelta(minutes=rec.duration_minutes)
    first_day = frm.astimezone(tz).date() - timedelta(days=(rec.duration_minutes + 1439) // 1440 + 1)
    last_day = to.astimezone(tz).date()
    out = []
    day = first_day
    while day <= last_day:
        if day.isoweekday() in rec.days and (rec.until is None or day <= rec.until):
            s = local_start(day, rec.start_time, tz)
            if s >= starts_at and s <= to and s + length >= frm:
                out.append((s, s + length))
        day += timedelta(days=1)
    return out


def active_at(starts_at: datetime, ends_at: datetime, rec: Optional[Recurrence], at: datetime) -> bool:
    return bool(occurrences(starts_at, ends_at, rec, at, at))


def next_occurrence(starts_at: datetime, ends_at: datetime, rec: Optional[Recurrence],
                    after: datetime) -> Optional[tuple[datetime, datetime]]:
    """The occurrence in force at ``after``, else the next one to start; None
    when the window has no occurrence left."""
    after = _aware(after)
    if rec is None:
        return (_aware(starts_at), _aware(ends_at)) if _aware(ends_at) >= after else None
    # A weekly series has an occurrence within any 8 days unless it is over;
    # one that begins later is looked up from its first occurrence.
    frm = max(after, _aware(starts_at))
    found = occurrences(starts_at, ends_at, rec, frm, frm + timedelta(days=8))
    return found[0] if found else None


def first_occurrence(rec: Recurrence, not_before: datetime) -> Optional[tuple[datetime, datetime]]:
    """The first occurrence of a new recurring window: the earliest one that
    has not ended by ``not_before`` (so a window created during an occurrence
    is in force at once). None when ``until`` leaves no occurrence."""
    not_before = _aware(not_before)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    found = occurrences(epoch, epoch, rec, not_before, not_before + timedelta(days=8))
    return found[0] if found else None


def describe(rec: Optional[Recurrence]) -> str:
    """"Every Mon, Wed at 02:00 for 2 h (Europe/Berlin) until 2026-12-31"."""
    if rec is None:
        return "One-off"
    days = "day" if len(rec.days) == 7 else ", ".join(DAY_NAMES[d] for d in rec.days)
    mins = rec.duration_minutes
    length = (f"{mins // 60} h {mins % 60} min" if mins % 60 else f"{mins // 60} h") if mins >= 60 else f"{mins} min"
    text_ = f"Every {days} at {rec.start_time.strftime('%H:%M')} for {length} ({rec.timezone})"
    return text_ + (f" until {rec.until.isoformat()}" if rec.until else "")


def valid_timezone(name: str) -> bool:
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# ─── SQL fragments (alias m = device_maintenance, d = devices, s = servers) ─

# "Window m covers device d". A group window covers the group's members and
# its subgroups' (primary, static and dynamic membership, migrate-192).
COVERS_DEVICE_SQL = """(
       (m.scope_type = 'device' AND m.scope_device_id = d.id)
    OR (m.scope_type = 'group'  AND (m.scope_group_id = d.group_id OR EXISTS (
           SELECT 1 FROM device_group_member_closure c
            WHERE c.group_id = m.scope_group_id AND c.asset_kind = 'device' AND c.asset_id = d.id)))
    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
    OR (m.scope_type = 'all')
)"""

# "Window m covers server s" (server scopes only; a linked device's windows
# are added by server_in_maintenance / active_server_ids).
COVERS_SERVER_SQL = """(
       (m.scope_type = 'server'       AND m.scope_server_id = s.id)
    OR (m.scope_type = 'server_group' AND EXISTS (
           SELECT 1 FROM device_group_member_closure c
            WHERE c.group_id = m.scope_group_id AND c.asset_kind = 'server' AND c.asset_id = s.id))
    OR (m.scope_type = 'server_tag'   AND jsonb_exists(COALESCE(s.tags, '[]'::jsonb), m.scope_tag))
)"""

ACTIVE_NOW_SQL = "device_maintenance_active(m, now())"
# Before migrate-207 there are only one-off windows.
_LEGACY_ACTIVE_NOW_SQL = "(m.starts_at <= now() AND m.ends_at >= now())"


def occurrences_join(frm: str = ":from_ts", to: str = ":to_ts", alias: str = "o") -> str:
    """``CROSS JOIN LATERAL`` giving each window's occurrences overlapping
    [frm, to] as ``<alias>.occ_start`` / ``<alias>.occ_end``. Use it after
    ``FROM device_maintenance m`` wherever a time range is asked about."""
    return f"CROSS JOIN LATERAL device_maintenance_occurrences(m, {frm}, {to}) {alias}"


_recurrence_supported: Optional[bool] = None


async def recurrence_supported(db: AsyncSession) -> bool:
    """Whether migrate-207 has run (cached once true; a database that is
    behind is asked again next time)."""
    global _recurrence_supported
    if _recurrence_supported:
        return True
    try:
        row = (await db.execute(text(
            "SELECT to_regprocedure('device_maintenance_active(device_maintenance, timestamp with time zone)')"
            " IS NOT NULL"))).first()
    except Exception:
        return False
    ok = bool(row and row[0] is True)
    if ok:
        _recurrence_supported = True
    return ok


async def active_now_sql(db: AsyncSession) -> str:
    """The SQL condition "window m is in force now" for this database (the
    one-off comparison before migrate-207)."""
    return ACTIVE_NOW_SQL if await recurrence_supported(db) else _LEGACY_ACTIVE_NOW_SQL


# ─── Evaluator helpers ──────────────────────────────────────────────────────

async def device_in_maintenance(db: AsyncSession, device_id: str) -> bool:
    """True for manual maintenance (devices.status) or a window in force that
    covers the device. Never raises: a failed lookup counts as not in
    maintenance, so alerting is not silenced by an error."""
    try:
        active = await active_now_sql(db)
        row = await db.execute(text(f"""
            SELECT 1
            FROM devices d
            WHERE d.id = :did AND (d.status = 'maintenance' OR EXISTS (
                SELECT 1 FROM device_maintenance m
                 WHERE {COVERS_DEVICE_SQL} AND {active}))
            LIMIT 1
        """), {"did": str(device_id)})
        return row.first() is not None
    except Exception:
        logger.debug("device maintenance lookup failed for %s", device_id, exc_info=True)
        return False


async def active_device_ids(db: AsyncSession) -> set[str]:
    """Devices in maintenance now (manual status or a window), in one query."""
    try:
        active = await active_now_sql(db)
        rows = (await db.execute(text(f"""
            SELECT d.id::text FROM devices d WHERE d.status = 'maintenance'
            UNION
            SELECT d.id::text
              FROM (SELECT * FROM device_maintenance m WHERE {active}) m
              JOIN devices d ON {COVERS_DEVICE_SQL}
        """))).all()
        return {r[0] for r in rows}
    except Exception:
        logger.debug("active maintenance device lookup failed", exc_info=True)
        return set()


def _server_sql(active: str, where: str) -> str:
    server_windows = (f"EXISTS (SELECT 1 FROM device_maintenance m WHERE {COVERS_SERVER_SQL} AND {active})"
                      if active == ACTIVE_NOW_SQL else "FALSE")
    return f"""
        SELECT s.id::text
          FROM servers s
          LEFT JOIN devices d ON d.id = s.device_id
         WHERE {where} AND (
               {server_windows}
            OR d.status = 'maintenance'
            OR (d.id IS NOT NULL AND EXISTS (
                   SELECT 1 FROM device_maintenance m WHERE {COVERS_DEVICE_SQL} AND {active})))
    """


async def server_in_maintenance(db: AsyncSession, server_id: str) -> bool:
    """True while a window in force covers the server (server, server group
    or server tag scope), or the device it is linked to is in maintenance.
    Never raises (a failed lookup counts as not in maintenance)."""
    try:
        active = await active_now_sql(db)
        row = await db.execute(text(_server_sql(active, "s.id = CAST(:sid AS uuid)") + " LIMIT 1"),
                               {"sid": str(server_id)})
        return row.first() is not None
    except Exception:
        logger.debug("server maintenance lookup failed for %s", server_id, exc_info=True)
        return False


async def active_server_ids(db: AsyncSession) -> set[str]:
    """Servers in maintenance now (see server_in_maintenance), in one query:
    for evaluators that walk every server once per pass."""
    try:
        active = await active_now_sql(db)
        rows = (await db.execute(text(_server_sql(active, "TRUE")))).all()
        return {r[0] for r in rows}
    except Exception:
        logger.debug("active maintenance server lookup failed", exc_info=True)
        return set()


def window_dict(row) -> dict:
    """The fields of a device_maintenance row the API and UI show about its
    timing: recurrence, active now, current or next occurrence."""
    def get(key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    rec = recurrence_of(row)
    now = datetime.now(UTC)
    nxt = next_occurrence(get("starts_at"), get("ends_at"), rec, now)
    return {
        "recurring": rec is not None,
        "recurrence": None if rec is None else {
            "days": list(rec.days), "start_time": rec.start_time.strftime("%H:%M"),
            "duration_minutes": rec.duration_minutes, "timezone": rec.timezone,
            "until": rec.until.isoformat() if rec.until else None,
        },
        "schedule": describe(rec),
        "active": active_at(get("starts_at"), get("ends_at"), rec, now),
        "next_starts_at": nxt[0] if nxt else None,
        "next_ends_at": nxt[1] if nxt else None,
    }
