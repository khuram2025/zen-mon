"""report_periods: rolling and calendar report windows in the organisation
timezone. The calendar cases mirror dashboard/scripts/test-report-periods.mjs
(features/reports/periods.ts), so a schedule delivers the same window the
viewer shows for the same period."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import report_periods as rp

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)          # Wednesday 14:05 in Riyadh


def iso(dt: datetime) -> str:
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def win(period, tz="Asia/Riyadh", week_start="monday", now=NOW):
    start, end, _label = rp.resolve_period(period, tz, week_start, now)
    return [iso(start), iso(end)]


# ── The dashboard's cases (test-report-periods.mjs) ─────────────────────────

def test_dashboard_cases():
    assert win("yesterday") == ["2026-10-05T21:00:00.000Z", "2026-10-06T21:00:00.000Z"]
    assert win("previous_week") == ["2026-09-27T21:00:00.000Z", "2026-10-04T21:00:00.000Z"]
    assert win("previous_week", week_start="sunday") == ["2026-09-26T21:00:00.000Z", "2026-10-03T21:00:00.000Z"]
    assert win("previous_month") == ["2026-08-31T21:00:00.000Z", "2026-09-30T21:00:00.000Z"]
    assert win("previous_quarter") == ["2026-06-30T21:00:00.000Z", "2026-09-30T21:00:00.000Z"]
    assert win("month_to_date") == ["2026-09-30T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]
    assert win("previous_month", "America/New_York", now=datetime(2026, 11, 15, 12, tzinfo=UTC)) == \
        ["2026-10-01T04:00:00.000Z", "2026-11-01T04:00:00.000Z"]


# ── Rolling windows end now ──────────────────────────────────────────────────

@pytest.mark.parametrize("period,hours", [("last_24h", 24), ("last_7d", 168), ("last_30d", 720), ("last_90d", 2160)])
def test_rolling_windows_end_now(period, hours):
    start, end, _ = rp.resolve_period(period, "Pacific/Auckland", "saturday", NOW)
    assert end == NOW and NOW - start == timedelta(hours=hours)


# ── Calendar windows ─────────────────────────────────────────────────────────

def test_to_date_windows_start_at_local_midnight_and_end_now():
    assert win("today") == ["2026-10-06T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]
    assert win("week_to_date") == ["2026-10-04T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]        # Mon 5 Oct
    assert win("week_to_date", week_start="sunday") == ["2026-10-03T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]
    assert win("week_to_date", week_start="saturday") == ["2026-10-02T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]
    assert win("quarter_to_date") == ["2026-09-30T21:00:00.000Z", "2026-10-07T11:05:00.000Z"]


@pytest.mark.parametrize("week_start,first_day", [("monday", 5), ("sunday", 4), ("saturday", 3)])
def test_week_start_on_its_own_first_day(week_start, first_day):
    # On the first day of the week, "this week" starts today and "previous
    # week" is the seven days before.
    now = datetime(2026, 10, first_day, 9, tzinfo=UTC)
    assert win("week_to_date", "UTC", week_start, now) == [f"2026-10-0{first_day}T00:00:00.000Z", iso(now)]
    start, end, _ = rp.resolve_period("previous_week", "UTC", week_start, now)
    assert end == datetime(2026, 10, first_day, tzinfo=UTC) and end - start == timedelta(days=7)


def test_year_boundaries():
    jan = datetime(2027, 1, 15, 12, tzinfo=UTC)
    assert win("previous_month", "UTC", now=jan) == ["2026-12-01T00:00:00.000Z", "2027-01-01T00:00:00.000Z"]
    assert win("previous_quarter", "UTC", now=jan) == ["2026-10-01T00:00:00.000Z", "2027-01-01T00:00:00.000Z"]
    assert win("quarter_to_date", "UTC", now=jan) == ["2027-01-01T00:00:00.000Z", iso(jan)]
    # 1 Jan 2027 is a Friday: the Monday week began in 2026.
    new_year = datetime(2027, 1, 1, 10, tzinfo=UTC)
    assert win("previous_week", "UTC", now=new_year) == ["2026-12-21T00:00:00.000Z", "2026-12-28T00:00:00.000Z"]
    assert win("yesterday", "UTC", now=new_year) == ["2026-12-31T00:00:00.000Z", "2027-01-01T00:00:00.000Z"]


def test_local_date_differs_from_the_utc_date():
    # 22:30 UTC on 30 Sep is already 1 Oct in Riyadh (and 30 Sep in New York).
    late = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)
    assert win("previous_month", "Asia/Riyadh", now=late) == ["2026-08-31T21:00:00.000Z", "2026-09-30T21:00:00.000Z"]
    assert win("previous_month", "America/New_York", now=late) == \
        ["2026-08-01T04:00:00.000Z", "2026-09-01T04:00:00.000Z"]
    # Half- and quarter-hour offsets.
    assert win("yesterday", "Asia/Kolkata") == ["2026-10-05T18:30:00.000Z", "2026-10-06T18:30:00.000Z"]
    assert win("yesterday", "Asia/Kathmandu") == ["2026-10-05T18:15:00.000Z", "2026-10-06T18:15:00.000Z"]


# ── Daylight saving: local midnights, not fixed 24-hour days ─────────────────

def test_windows_across_a_dst_change_follow_the_wall_clock():
    # Europe/London leaves BST on Sunday 25 Oct 2026: that week is 7 days + 1 hour.
    start, end, _ = rp.resolve_period("previous_week", "Europe/London", "monday",
                                      datetime(2026, 10, 28, 12, tzinfo=UTC))
    assert (iso(start), iso(end)) == ("2026-10-18T23:00:00.000Z", "2026-10-26T00:00:00.000Z")
    assert end - start == timedelta(days=7, hours=1)
    # New York enters DST on 8 Mar 2026: March is one hour short.
    start, end, _ = rp.resolve_period("previous_month", "America/New_York", "monday",
                                      datetime(2026, 4, 10, 12, tzinfo=UTC))
    assert (iso(start), iso(end)) == ("2026-03-01T05:00:00.000Z", "2026-04-01T04:00:00.000Z")
    assert end - start == timedelta(days=31) - timedelta(hours=1)
    # Southern hemisphere: Sydney starts DST on 4 Oct 2026.
    start, end, _ = rp.resolve_period("previous_month", "Australia/Sydney", "monday",
                                      datetime(2026, 11, 10, 12, tzinfo=UTC))
    assert (iso(start), iso(end)) == ("2026-09-30T14:00:00.000Z", "2026-10-31T13:00:00.000Z")
    # The day of the change itself is 23 hours long.
    start, end, _ = rp.resolve_period("yesterday", "America/New_York", "monday",
                                      datetime(2026, 3, 9, 15, tzinfo=UTC))
    assert (iso(start), iso(end)) == ("2026-03-08T05:00:00.000Z", "2026-03-09T04:00:00.000Z")


def test_a_midnight_skipped_by_dst_starts_the_day_at_its_first_instant():
    # America/Santiago jumps from 00:00 to 01:00 on 6 Sep 2026: that day starts
    # at 01:00 local (04:00 UTC), not at 23:00 the evening before.
    start, end, _ = rp.resolve_period("today", "America/Santiago", "monday",
                                      datetime(2026, 9, 6, 15, tzinfo=UTC))
    assert iso(start) == "2026-09-06T04:00:00.000Z"
    assert start.astimezone(rp._zone("America/Santiago")).date().isoformat() == "2026-09-06"


# ── Labels ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("period,label", [
    ("previous_month", "Previous month (1 Sep 2026 – 30 Sep 2026)"),
    ("previous_quarter", "Previous quarter (1 Jul 2026 – 30 Sep 2026)"),
    ("previous_week", "Previous week (28 Sep 2026 – 4 Oct 2026)"),
    ("yesterday", "Yesterday (6 Oct 2026)"),
    ("today", "Today (7 Oct 2026)"),
    ("week_to_date", "This week (5 Oct 2026 – 7 Oct 2026)"),
    ("month_to_date", "This month (1 Oct 2026 – 7 Oct 2026)"),
    ("quarter_to_date", "This quarter (1 Oct 2026 – 7 Oct 2026)"),
    ("last_24h", "Last 24 hours (6 Oct 2026 – 7 Oct 2026)"),
    ("last_7d", "Last 7 days (30 Sep 2026 – 7 Oct 2026)"),
])
def test_labels_name_the_local_days_covered(period, label):
    assert rp.resolve_period(period, "Asia/Riyadh", "monday", NOW)[2] == label


# ── Inputs ───────────────────────────────────────────────────────────────────

def test_inputs_are_sanitised():
    naive = NOW.replace(tzinfo=None)
    assert rp.resolve_period("yesterday", "Asia/Riyadh", "monday", naive) == \
        rp.resolve_period("yesterday", "Asia/Riyadh", "monday", NOW)
    assert win("yesterday", "Not/AZone") == win("yesterday", "UTC")
    assert win("yesterday", "") == win("yesterday", "UTC")
    assert win("previous_week", week_start="funday") == win("previous_week", week_start="monday")
    assert win("previous_week", week_start="Sunday") == win("previous_week", week_start="sunday")
    with pytest.raises(ValueError):
        rp.resolve_period("custom", "UTC")
    start, end, _ = rp.resolve_period("last_24h", "UTC")
    assert end.tzinfo is not None and abs(datetime.now(UTC) - end) < timedelta(seconds=5)


def test_period_vocabulary_matches_the_dashboard_and_catalog():
    from app.services import report_templates

    assert rp.PERIOD_IDS == tuple(report_templates.PERIODS)
    ts = Path(__file__).resolve().parents[2] / "dashboard/src/features/reports/periods.ts"
    if not ts.exists():
        pytest.skip("dashboard sources not present")
    found = re.findall(r"\{ id: '([a-z0-9_]+)', label: '([^']+)'", ts.read_text())
    assert tuple(found) == tuple((pid, label) for pid, label, _kind in rp.PERIODS)


# ── Previous work week (five days from the organisation week start) ─────────

def test_previous_work_week():
    # Wednesday 7 Oct: the current work week has not ended, so last week's.
    assert win("previous_work_week", week_start="sunday") == \
        ["2026-09-26T21:00:00.000Z", "2026-10-01T21:00:00.000Z"]            # Sun 27 Sep – Thu 1 Oct
    assert win("previous_work_week", week_start="monday") == \
        ["2026-09-27T21:00:00.000Z", "2026-10-02T21:00:00.000Z"]            # Mon 28 Sep – Fri 2 Oct
    # From the day after the last work day, this week's.
    friday = datetime(2026, 10, 9, 5, tzinfo=UTC)                         # Fri 08:00 Riyadh
    assert win("previous_work_week", week_start="sunday", now=friday) == \
        ["2026-10-03T21:00:00.000Z", "2026-10-08T21:00:00.000Z"]            # Sun 4 – Thu 8 Oct
    thursday = datetime(2026, 10, 8, 5, tzinfo=UTC)
    assert win("previous_work_week", week_start="sunday", now=thursday) == \
        ["2026-09-26T21:00:00.000Z", "2026-10-01T21:00:00.000Z"]
    _s, _e, label = rp.resolve_period("previous_work_week", "Asia/Riyadh", "sunday", NOW)
    assert label == "Previous work week (27 Sep 2026 – 1 Oct 2026)"
