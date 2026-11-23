"""A discovery rule's own schedule (app.services.discovery_rules.schedules): pure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.discovery_rules import schedules as sc

UTC = timezone.utc


def test_validate_normalises_and_refuses():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert sc.validate(None) is None
    assert sc.validate({"kind": "weekly", "days": [4, 0, 0], "time": "02:00", "timezone": "Asia/Riyadh"}) == \
        {"kind": "weekly", "days": [0, 4], "time": "02:00", "timezone": "Asia/Riyadh"}
    assert sc.validate({"kind": "once", "at": "2026-12-01T22:00:00", "timezone": "UTC"}, now=now)["at"] == \
        "2026-12-01T22:00"
    for bad, msg in [({"kind": "yearly"}, "once, hourly"),
                     ({"kind": "hourly", "every_hours": 5, "time": "00:00"}, "Every"),
                     ({"kind": "weekly", "days": [], "time": "02:00"}, "at least one day"),
                     ({"kind": "weekly", "days": [7], "time": "02:00"}, "at least one day"),
                     ({"kind": "monthly", "day": 29, "time": "02:00"}, "1 to 28"),
                     ({"kind": "weekly", "days": [1], "time": "2am"}, "HH:MM"),
                     ({"kind": "weekly", "days": [1], "time": "02:00", "timezone": "Mars/Base"}, "time zone"),
                     ({"kind": "once", "at": "2026-11-23T11:00", "timezone": "UTC"}, "future")]:
        with pytest.raises(sc.ScheduleError, match=msg):
            sc.validate(bad, now=now)


def test_weekly_slots_follow_local_time_across_dst():
    s = {"kind": "weekly", "days": [6], "time": "02:30", "timezone": "Europe/Berlin"}   # Sundays
    # Sunday 2026-03-29 is the spring-forward day in Berlin: 02:30 does not
    # exist and is read as 03:30 CEST (01:30 UTC); the slot before is 01:30 UTC
    # on 2026-03-22 (CET).
    now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    assert sc.last_slot(now, s) == datetime(2026, 3, 22, 1, 30, tzinfo=UTC)
    nxt = sc.next_slot(now, s)
    assert nxt.date().isoformat() == "2026-03-29" and nxt > now
    # autumn: 02:30 local on 2026-10-25 is CEST before the change
    assert sc.next_slot(datetime(2026, 10, 24, 12, tzinfo=UTC), s).isoformat().startswith("2026-10-25T00:30")


def test_monthly_and_hourly_and_once():
    m = {"kind": "monthly", "day": 15, "time": "03:00", "timezone": "UTC"}
    assert sc.last_slot(datetime(2026, 11, 14, tzinfo=UTC), m) == datetime(2026, 10, 15, 3, tzinfo=UTC)
    assert sc.next_slot(datetime(2026, 12, 20, tzinfo=UTC), m) == datetime(2027, 1, 15, 3, tzinfo=UTC)
    h = {"kind": "hourly", "every_hours": 6, "time": "00:30", "timezone": "Asia/Riyadh"}
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)            # 13:00 Riyadh
    assert sc.last_slot(now, h) == datetime(2026, 11, 23, 9, 30, tzinfo=UTC)     # 12:30 Riyadh
    assert sc.next_slot(now, h) == datetime(2026, 11, 23, 15, 30, tzinfo=UTC)    # 18:30 Riyadh
    o = {"kind": "once", "at": "2026-12-01T22:00", "timezone": "Asia/Riyadh"}
    at = datetime(2026, 12, 1, 19, 0, tzinfo=UTC)
    assert sc.last_slot(at - timedelta(minutes=1), o) is None and sc.next_slot(at - timedelta(minutes=1), o) == at
    assert sc.last_slot(at, o) == at and sc.next_slot(at, o) is None


def test_due_runs_each_slot_once_and_recurring_needs_a_starting_point():
    s = {"kind": "weekly", "days": list(range(7)), "time": "02:00", "timezone": "UTC"}
    now = datetime(2026, 11, 23, 2, 5, tzinfo=UTC)
    slot = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)
    assert sc.due(s, None, now) is None                           # no starting point yet: not run
    assert sc.starting_point(s, now) == slot
    assert sc.due(s, slot - timedelta(days=1), now) == slot
    assert sc.due(s, slot, now) is None
    o = {"kind": "once", "at": "2026-11-23T02:00", "timezone": "UTC"}
    assert sc.starting_point(o, now) is None and sc.due(o, None, now) == slot and sc.due(o, slot, now) is None


def test_describe():
    assert sc.describe(None) == "Shared schedule"
    assert sc.describe({"kind": "weekly", "days": list(range(7)), "time": "02:00", "timezone": "UTC"}) == \
        "Daily at 02:00 (UTC)"
    assert sc.describe({"kind": "weekly", "days": [0, 2], "time": "02:00", "timezone": "UTC"}) == \
        "Mon, Wed at 02:00 (UTC)"
    assert sc.describe({"kind": "hourly", "every_hours": 1, "time": "00:00", "timezone": "UTC"}) == \
        "Every hour from 00:00 (UTC)"
    assert sc.describe({"kind": "monthly", "day": 1, "time": "03:00", "timezone": "UTC"}) == \
        "Monthly on day 1 at 03:00 (UTC)"
    assert sc.describe({"kind": "once", "at": "2026-12-01T22:00", "timezone": "UTC"}) == \
        "Once on 2026-12-01 at 22:00 (UTC)"
