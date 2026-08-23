"""Alert wording: metric phrasing, durations, and the shared defaults.

These assertions are the contract every notification channel leans on. The one
thing they all guard: a person, not a parser, reads the output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import alert_phrasing as ap


@pytest.mark.parametrize("metric,operator,threshold,expected", [
    ("if_util_pct", ">", 80.0, "Interface utilisation above 80%"),
    ("rtt", ">", 200, "Round-trip time above 200 ms"),
    ("cpu", ">=", 90, "Device CPU at or above 90%"),
    ("apm_apdex", "<", 0.85, "Apdex score below 0.85"),
    # Fractions are stored 0..1 and read as percentages.
    ("apm_error_rate", ">", 0.02, "Error rate above 2%"),
    # Bit rates pick their own SI step.
    ("if_in_bps", ">", 1_500_000_000, "Inbound traffic above 1.5 Gbps"),
    ("if_in_bps", ">", 250_000_000, "Inbound traffic above 250 Mbps"),
    ("temperature", ">", 65, "Device temperature above 65°C"),
])
def test_numeric_conditions_read_as_phrases(metric, operator, threshold, expected):
    assert ap.condition_label(metric, operator, threshold) == expected


@pytest.mark.parametrize("metric,operator,threshold,expected", [
    # Flag metrics disagree about which number is healthy; each is labelled by
    # the state it actually watches, not by its raw comparison.
    ("ping_status", "==", 0, "Device unreachable (no ICMP response)"),
    ("ping_status", "==", 1, "Device reachable"),
    ("if_oper_status", "==", 2, "Interface operationally down"),
    ("service_status", "==", 0, "Service check failing"),
    ("uptime_reset", "!=", 0, "Device reboot detected"),
    ("host_service_down", "==", 1, "Monitored service stopped"),
])
def test_state_conditions_name_the_state(metric, operator, threshold, expected):
    assert ap.condition_label(metric, operator, threshold) == expected


@pytest.mark.parametrize("operator,expected", [
    # Rules created by the wizard store symbols; the link-utilization quick-rule
    # stores names. Both describe the same condition.
    (">", "Interface utilisation above 90%"),
    ("gt", "Interface utilisation above 90%"),
    ("gte", "Interface utilisation at or above 90%"),
    ("lt", "Interface utilisation below 90%"),
    # "is" carries the whole comparison for equality, so it stays.
    ("eq", "Interface utilisation is 90%"),
    ("neq", "Interface utilisation other than 90%"),
])
def test_named_and_symbolic_operators_agree(operator, expected):
    assert ap.condition_label("if_util_pct", operator, 90) == expected


def test_named_operators_pick_the_right_state_side():
    assert ap.condition_label("if_oper_status", "eq", 2) == "Interface operationally down"
    assert ap.condition_label("uptime_reset", "eq", 1) == "Device reboot detected"


def test_unknown_and_template_metrics_degrade_gracefully():
    assert ap.condition_label("tpl_ups_load_pct", ">", 70) == "Ups load pct above 70"
    assert ap.condition_label("nonsense_key", ">", 1) == "Nonsense key above 1"


@pytest.mark.parametrize("seconds,expected", [
    (3, "a few seconds"),
    (45, "45 seconds"),
    (60, "1 minute"),
    (205, "3 minutes 25 seconds"),
    # Past ten minutes the seconds stop informing any decision.
    (610, "10 minutes"),
    (4500, "1 hour 15 minutes"),
    (86_400, "1 day"),
    (300_000, "3 days 11 hours"),
])
def test_durations_are_readable(seconds, expected):
    assert ap.humanize_duration(seconds) == expected


def test_duration_between_handles_naive_and_iso_timestamps():
    ended = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    started = ended - timedelta(minutes=12)
    assert ap.duration_between(started, ended) == "12 minutes"
    # A naive timestamp out of Postgres is read as UTC rather than crashing.
    assert ap.duration_between(started.replace(tzinfo=None), ended) == "12 minutes"
    assert ap.duration_between(started.isoformat(), ended.isoformat()) == "12 minutes"
    assert ap.duration_between(None) == ""


def test_event_sentence_names_the_subject_once():
    v = ap.phrasing_variables(metric="if_util_pct", operator=">", threshold=80,
                              reading="87%", subject_noun="core-router-01")
    assert v["event_sentence"] == (
        "Interface utilisation on core-router-01 rose above 80% (currently 87%)."
    )

    # A state metric puts the subject first and keeps the host's own casing.
    v = ap.phrasing_variables(metric="ping_status", operator="==", threshold=0,
                              subject_noun="core-router-01")
    assert v["event_sentence"] == "core-router-01 is not responding to ping."


def test_recovery_phrasing_flips_the_verb_and_carries_the_duration():
    v = ap.phrasing_variables(metric="if_util_pct", operator=">", threshold=80,
                              reading="42%", subject_noun="core-router-01",
                              is_recovery=True, duration="12 minutes")
    assert v["event_sentence"] == (
        "Interface utilisation on core-router-01 is back below 80% (now 42%)."
    )
    assert v["duration_sentence"] == " The condition was active for 12 minutes."
    assert v["duration_suffix"] == " Active for 12 minutes."


def test_multi_condition_rules_join_on_their_logic():
    conds = [
        {"metric": "cpu", "operator": ">", "threshold": 90},
        {"metric": "memory", "operator": ">", "threshold": 85},
    ]
    assert ap.conditions_label(conds, "AND") == (
        "Device CPU above 90% and Device memory above 85%"
    )
    assert ap.conditions_label(conds, "OR") == (
        "Device CPU above 90% or Device memory above 85%"
    )
    # Empty array: fall back to the rule's own metric columns.
    assert ap.conditions_label([], "AND", metric="cpu", operator=">", threshold=90) == (
        "Device CPU above 90%"
    )


def test_threshold_placeholder_stays_raw_for_older_templates():
    """A stored template saying "{threshold}%" must not render "80%%"."""
    v = ap.phrasing_variables(metric="if_util_pct", operator=">", threshold=80.0)
    assert v["threshold"] == "80.0"
    assert v["threshold_value"] == "80%"


def test_sample_readings_sit_the_right_side_of_the_threshold():
    assert ap.sample_reading("if_util_pct", ">", 80) == "87%"
    assert ap.sample_reading("if_util_pct", ">", 80, recovered=True) == "42%"
    # A "too low" rule breaches below the threshold and recovers above it.
    assert ap.sample_reading("apm_apdex", "<", 0.9) == "0.55"
    assert ap.sample_reading("apm_apdex", "<", 0.9, recovered=True) == "1.12"
    # Percentages never exceed 100, and never land ON it either — "currently
    # 100%" beside "above 98%" reads like a rounding bug.
    assert ap.sample_reading("cpu", ">", 98) == "99%"
    assert ap.sample_reading("host_filesystem_pct", "gt", 95) == "98%"
    # State metrics have no number worth inventing.
    assert ap.sample_reading("ping_status", "==", 0) == ""
