"""SLA escalation planning (alert_escalation_service.plan_escalation).

The sweeper decides, per active unacknowledged alert, whether a new escalation
level is due, whether the current level should repeat, or whether to stay
quiet. These pin the semantics the wizard advertises: levels fire on their
deadlines, only the highest overdue level pages when several became due at
once, repeats respect their cadence, and max_repeat caps them.
"""
from datetime import datetime, timedelta, timezone

from app.services.alert_escalation_service import (
    escalated_channel_ids,
    plan_escalation,
)

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def _levels(*deadlines, repeat=None):
    return [
        {"after_minutes": d, "notify_channels": [f"ch-{i}"],
         "repeat_every_minutes": repeat if i == len(deadlines) - 1 else None}
        for i, d in enumerate(deadlines)
    ]


def test_no_levels_never_escalates():
    assert plan_escalation([], None, _ago(120), NOW) is None
    assert plan_escalation(_levels(15), None, None, NOW) is None


def test_before_first_deadline_stays_quiet():
    assert plan_escalation(_levels(15, 30), None, _ago(10), NOW) is None


def test_first_deadline_fires_level_one():
    assert plan_escalation(_levels(15, 30), None, _ago(16), NOW) == ("escalate", 1)


def test_deadline_is_inclusive():
    assert plan_escalation(_levels(15), None, _ago(15), NOW) == ("escalate", 1)


def test_second_deadline_fires_level_two():
    state = {"level": 1, "last_at": _ago(15).isoformat(), "repeats": 0}
    assert plan_escalation(_levels(15, 30), state, _ago(31), NOW) == ("escalate", 2)


def test_already_escalated_level_does_not_refire():
    state = {"level": 1, "last_at": _ago(1).isoformat(), "repeats": 0}
    assert plan_escalation(_levels(15, 30), state, _ago(20), NOW) is None


def test_multiple_overdue_levels_page_only_the_highest():
    """Sweeper downtime / a long outage discovered late must not page every
    tier in the same minute."""
    assert plan_escalation(_levels(15, 30, 60), None, _ago(90), NOW) == ("escalate", 3)


def test_repeat_fires_on_cadence():
    levels = _levels(15, repeat=10)
    state = {"level": 1, "last_at": _ago(11).isoformat(), "repeats": 0}
    assert plan_escalation(levels, state, _ago(30), NOW) == ("repeat", 1)


def test_repeat_waits_for_its_cadence():
    levels = _levels(15, repeat=10)
    state = {"level": 1, "last_at": _ago(5).isoformat(), "repeats": 0}
    assert plan_escalation(levels, state, _ago(30), NOW) is None


def test_level_without_repeat_never_repeats():
    levels = _levels(15)  # repeat=None
    state = {"level": 1, "last_at": _ago(120).isoformat(), "repeats": 0}
    assert plan_escalation(levels, state, _ago(180), NOW) is None


def test_max_repeat_caps_repeats():
    levels = _levels(15, repeat=10)
    state = {"level": 1, "last_at": _ago(30).isoformat(), "repeats": 3}
    assert plan_escalation(levels, state, _ago(60), NOW, max_repeat=3) is None
    # 0 = unlimited
    assert plan_escalation(levels, state, _ago(60), NOW, max_repeat=0) == ("repeat", 1)


def test_escalation_beats_pending_repeat():
    """When level 2's deadline arrives, escalate rather than repeat level 1."""
    levels = _levels(15, 30, repeat=10)
    state = {"level": 1, "last_at": _ago(12).isoformat(), "repeats": 0}
    assert plan_escalation(levels, state, _ago(31), NOW) == ("escalate", 2)


def test_naive_triggered_at_is_treated_as_utc():
    naive = _ago(20).replace(tzinfo=None)
    assert plan_escalation(_levels(15), None, naive, NOW) == ("escalate", 1)


def test_future_triggered_at_stays_quiet():
    assert plan_escalation(_levels(15), None, NOW + timedelta(minutes=5), NOW) is None


def test_escalated_channel_ids_dedupes_in_level_order():
    levels = [
        {"after_minutes": 15, "notify_channels": ["a", "b"]},
        {"after_minutes": 30, "notify_channels": ["b", "c"]},
        {"after_minutes": 60, "notify_channels": ["d"]},
    ]
    assert escalated_channel_ids(levels, 2) == ["a", "b", "c"]
    assert escalated_channel_ids(levels, 3) == ["a", "b", "c", "d"]
    assert escalated_channel_ids(levels, 0) == []
