"""Maintenance windows: occurrence math and request validation (migrate-207).

A recurring window repeats weekly on chosen days at a local start time for a
number of absolute minutes, in an IANA zone, optionally until a local date.
These are the pure functions the API uses for the next occurrence and the
first occurrence it stores; ``test_maintenance_windows_pg`` checks that the SQL
function every evaluator uses gives the same answers.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest

from app.services import maintenance_windows as mw

UTC = timezone.utc
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
ALL_DAYS = (1, 2, 3, 4, 5, 6, 7)


def rec(days=ALL_DAYS, start="02:00", minutes=60, tz="Europe/Berlin", until=None):
    return mw.Recurrence(days=tuple(days), start_time=time.fromisoformat(start),
                         duration_minutes=minutes, timezone=tz, until=until)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def active(r, at, starts=EPOCH):
    return mw.active_at(starts, starts, r, at)


# ── One-off windows are unchanged ───────────────────────────────────────────

def test_one_off_window_is_inclusive_at_both_ends():
    s, e = utc(2026, 11, 19, 2), utc(2026, 11, 19, 3)
    assert mw.active_at(s, e, None, s) and mw.active_at(s, e, None, e)
    assert not mw.active_at(s, e, None, e + timedelta(seconds=1))
    assert not mw.active_at(s, e, None, s - timedelta(seconds=1))
    assert mw.occurrences(s, e, None, utc(2026, 11, 1), utc(2026, 12, 1)) == [(s, e)]
    assert mw.next_occurrence(s, e, None, utc(2026, 11, 19, 2, 30)) == (s, e)
    assert mw.next_occurrence(s, e, None, utc(2026, 11, 19, 4)) is None


# ── Recurring windows ───────────────────────────────────────────────────────

def test_nightly_backup_window_in_local_time():
    """02:00-03:00 Berlin every night: 01:00-02:00 UTC in winter (CET)."""
    r = rec(start="02:00", minutes=60)
    assert active(r, utc(2026, 11, 19, 1, 0))
    assert active(r, utc(2026, 11, 19, 1, 30))
    assert active(r, utc(2026, 11, 19, 2, 0))           # inclusive end, like one-off windows
    assert not active(r, utc(2026, 11, 19, 2, 1))
    assert not active(r, utc(2026, 11, 19, 0, 59))
    # Summer (CEST): 00:00-01:00 UTC.
    assert active(r, utc(2026, 7, 1, 0, 30))
    assert not active(r, utc(2026, 7, 1, 1, 30))


def test_only_the_chosen_weekdays():
    r = rec(days=(1, 3), start="09:00", minutes=30, tz="UTC")      # Mon, Wed
    assert active(r, utc(2026, 11, 16, 9, 15))                    # Monday
    assert not active(r, utc(2026, 11, 17, 9, 15))                # Tuesday
    assert active(r, utc(2026, 11, 18, 9, 15))                    # Wednesday


def test_window_spanning_midnight_belongs_to_its_start_day():
    """Saturday 23:00 for 3 h covers Sunday 00:00-02:00, but Sunday's own
    23:00 does not start one (Sunday is not chosen)."""
    r = rec(days=(6,), start="23:00", minutes=180, tz="UTC")
    assert active(r, utc(2026, 11, 21, 23, 30))                   # Sat
    assert active(r, utc(2026, 11, 22, 1, 59))                    # Sun, from Saturday's occurrence
    assert not active(r, utc(2026, 11, 22, 2, 1))
    assert not active(r, utc(2026, 11, 22, 23, 30))               # Sun 23:30: no occurrence
    assert not active(r, utc(2026, 11, 21, 22, 59))


def test_week_long_window_reaches_back_seven_days():
    r = rec(days=(1,), start="00:00", minutes=mw.MAX_DURATION_MINUTES, tz="UTC")
    monday = utc(2026, 11, 16)
    assert active(r, monday + timedelta(days=6, hours=23, minutes=59))
    assert active(r, monday + timedelta(days=7))                  # next Monday: next occurrence starts
    occ = mw.occurrences(EPOCH, EPOCH, r, monday + timedelta(days=3), monday + timedelta(days=3))
    assert occ == [(monday, monday + timedelta(days=7))]


def test_dst_spring_forward_keeps_the_absolute_duration():
    """Berlin, 29 Mar 2026: clocks jump 02:00 -> 03:00. A 01:30 window of 120
    minutes runs 00:30-02:30 UTC (01:30 CET to 04:30 CEST) — two real hours."""
    r = rec(start="01:30", minutes=120)
    [(s, e)] = mw.occurrences(EPOCH, EPOCH, r, utc(2026, 3, 29, 1), utc(2026, 3, 29, 1))
    assert s == utc(2026, 3, 29, 0, 30) and e == utc(2026, 3, 29, 2, 30)
    assert e - s == timedelta(hours=2)


def test_dst_nonexistent_local_start_uses_the_offset_before_the_change():
    """02:30 does not exist in Berlin on 29 Mar 2026; like PostgreSQL it is
    read with the offset in force before the change (CET): 01:30 UTC."""
    r = rec(start="02:30", minutes=30)
    [(s, _e)] = mw.occurrences(EPOCH, EPOCH, r, utc(2026, 3, 29, 1, 40), utc(2026, 3, 29, 1, 40))
    assert s == utc(2026, 3, 29, 1, 30)


def test_dst_fall_back_ambiguous_local_start_uses_the_offset_after_the_change():
    """02:30 happens twice in Berlin on 25 Oct 2026; like PostgreSQL the
    second (CET) reading is used: 01:30 UTC, once."""
    r = rec(start="02:30", minutes=30)
    occ = mw.occurrences(EPOCH, EPOCH, r, utc(2026, 10, 25), utc(2026, 10, 25, 23, 59))
    assert occ == [(utc(2026, 10, 25, 1, 30), utc(2026, 10, 25, 2, 0))]
    # The day after is a normal CET day.
    assert mw.occurrences(EPOCH, EPOCH, r, utc(2026, 10, 26), utc(2026, 10, 26, 23, 59)) == [
        (utc(2026, 10, 26, 1, 30), utc(2026, 10, 26, 2, 0))]


def test_until_date_is_the_last_local_start_day():
    r = rec(start="23:00", minutes=120, tz="UTC", until=date(2026, 11, 20))
    assert active(r, utc(2026, 11, 21, 0, 30))                    # the 20th's occurrence runs on
    assert not active(r, utc(2026, 11, 21, 23, 30))               # the 21st starts none
    assert mw.next_occurrence(EPOCH, EPOCH, r, utc(2026, 11, 21, 2)) is None


def test_occurrences_before_the_first_one_do_not_count():
    r = rec(start="02:00", minutes=60, tz="UTC")
    first = utc(2026, 11, 20, 2)
    assert not mw.active_at(first, first + timedelta(hours=1), r, utc(2026, 11, 19, 2, 30))
    assert mw.active_at(first, first + timedelta(hours=1), r, utc(2026, 11, 20, 2, 30))


def test_first_occurrence_includes_one_in_progress():
    r = rec(start="02:00", minutes=120, tz="UTC")
    assert mw.first_occurrence(r, utc(2026, 11, 19, 3)) == (utc(2026, 11, 19, 2), utc(2026, 11, 19, 4))
    assert mw.first_occurrence(r, utc(2026, 11, 19, 5)) == (utc(2026, 11, 20, 2), utc(2026, 11, 20, 4))
    weekly = rec(days=(7,), start="02:00", minutes=60, tz="UTC")              # Sundays
    assert mw.first_occurrence(weekly, utc(2026, 11, 19, 5))[0] == utc(2026, 11, 22, 2)
    gone = rec(days=(7,), start="02:00", minutes=60, tz="UTC", until=date(2026, 11, 20))
    assert mw.first_occurrence(gone, utc(2026, 11, 19, 5)) is None


def test_next_occurrence_is_the_current_one_while_in_force():
    r = rec(days=(2, 4), start="02:00", minutes=60, tz="UTC")                # Tue, Thu
    first = mw.first_occurrence(r, utc(2026, 11, 16))
    assert first[0] == utc(2026, 11, 17, 2)
    assert mw.next_occurrence(first[0], first[1], r, utc(2026, 11, 19, 2, 30)) == (
        utc(2026, 11, 19, 2), utc(2026, 11, 19, 3))
    assert mw.next_occurrence(first[0], first[1], r, utc(2026, 11, 19, 4))[0] == utc(2026, 11, 24, 2)
    # Before the series begins: its first occurrence.
    assert mw.next_occurrence(first[0], first[1], r, utc(2026, 11, 1)) == first


def test_describe_reads_plainly():
    assert mw.describe(None) == "One-off"
    assert mw.describe(rec(start="02:00", minutes=120)) == "Every day at 02:00 for 2 h (Europe/Berlin)"
    assert mw.describe(rec(days=(1, 3), start="22:30", minutes=90, tz="UTC", until=date(2026, 12, 31))) == \
        "Every Mon, Wed at 22:30 for 1 h 30 min (UTC) until 2026-12-31"


def test_window_dict_reports_timing():
    first = mw.first_occurrence(rec(tz="UTC"), datetime.now(UTC))
    row = {"starts_at": first[0], "ends_at": first[1], "recur_days": [1, 2, 3, 4, 5, 6, 7],
           "recur_start_time": time(2), "recur_duration_minutes": 60, "recur_timezone": "UTC",
           "recur_until": None}
    d = mw.window_dict(row)
    assert d["recurring"] and d["recurrence"]["start_time"] == "02:00"
    assert d["next_starts_at"] is not None and d["next_ends_at"] - d["next_starts_at"] == timedelta(hours=1)


# ── Request validation ──────────────────────────────────────────────────────

def test_recurrence_request_validation():
    from pydantic import ValidationError
    from app.schemas.device import DeviceMaintenanceCreate

    base = {"scope_type": "device", "scope_device_id": str(uuid4())}
    ok = DeviceMaintenanceCreate(**base, recurrence={"days": [3, 1, 1], "start_time": "02:00:45",
                                                     "duration_minutes": 60})
    assert ok.recurrence.days == [1, 3] and ok.recurrence.start_time == time(2, 0)
    assert ok.recurrence.timezone is None                      # the organisation's, filled by the API
    for bad in ({"days": [], "start_time": "02:00", "duration_minutes": 60},
                {"days": [0], "start_time": "02:00", "duration_minutes": 60},
                {"days": [8], "start_time": "02:00", "duration_minutes": 60},
                {"days": [1], "start_time": "02:00", "duration_minutes": 0},
                {"days": [1], "start_time": "02:00", "duration_minutes": 7 * 24 * 60 + 1},
                {"days": [1], "start_time": "02:00", "duration_minutes": 60, "timezone": "Mars/Olympus"}):
        with pytest.raises(ValidationError):
            DeviceMaintenanceCreate(**base, recurrence=bad)
    assert DeviceMaintenanceCreate(**base, recurrence={"days": [1], "start_time": "02:00",
                                                       "duration_minutes": 7 * 24 * 60}).recurrence
    # A recurring window does not take an end; a one-off one still needs both ends.
    with pytest.raises(ValidationError):
        DeviceMaintenanceCreate(**base, ends_at=datetime.now(UTC),
                                recurrence={"days": [1], "start_time": "02:00", "duration_minutes": 60})
    with pytest.raises(ValidationError):
        DeviceMaintenanceCreate(**base, starts_at=datetime.now(UTC))


def test_server_scope_request_validation():
    from pydantic import ValidationError
    from app.schemas.device import DeviceMaintenanceCreate

    now = datetime.now(UTC)
    span = {"starts_at": now, "ends_at": now + timedelta(hours=1)}
    assert DeviceMaintenanceCreate(scope_type="server", scope_server_id=str(uuid4()), **span)
    assert DeviceMaintenanceCreate(scope_type="server_tag", scope_tag="backup", **span)
    assert DeviceMaintenanceCreate(scope_type="server_group", scope_group_id=str(uuid4()), **span)
    for bad in ({"scope_type": "server"}, {"scope_type": "server_tag", "scope_tag": " "},
                {"scope_type": "server_group"}, {"scope_type": "everything"}):
        with pytest.raises(ValidationError):
            DeviceMaintenanceCreate(**bad, **span)
