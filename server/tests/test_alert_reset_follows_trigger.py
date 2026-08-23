"""Reset actions run only for the rule whose alert the recovery closes.

The ping path's recovery branch used to loop over every enabled rule and, for
each 'any'-scoped one, send its reset actions — the metric-threshold gate that
protects the trigger side is deliberately bypassed for recoveries. So a device
flapping degraded for a minute fired the reset actions of every unscoped rule
on the appliance, including SNMP metric rules ("High memory usage") that never
triggered and are evaluated elsewhere entirely: the operator got "resolved"
mail about incidents that never happened.

The recovery UPDATE now RETURNs the alert rows it actually closed, and
_recovery_map turns them into the gate: no closed row for your rule, no reset.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.v1.alert_engine import _recovery_map


def _row(rule_id="rule-1", minutes_ago=10, notified=None):
    return SimpleNamespace(
        rule_id=rule_id,
        triggered_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        notified=notified,
    )


def test_a_recovery_that_closed_nothing_gates_every_reset():
    """The reported bug: reset mail for a rule that never triggered."""
    assert _recovery_map([]) == {}


def test_only_rules_with_a_closed_alert_are_present():
    out = _recovery_map([_row("rule-1")])
    assert "rule-1" in out
    assert "rule-2" not in out


def test_absent_flag_counts_as_notified_for_upgrade_safety():
    """Rows predating the notified flag must still get their all-clear."""
    assert _recovery_map([_row(notified=None)])["rule-1"]["notified"] is True


def test_suppressed_trigger_suppresses_the_reset():
    assert _recovery_map([_row(notified="false")])["rule-1"]["notified"] is False


def test_any_notified_row_wins_across_a_rules_stacked_alerts():
    rows = [_row(notified="false", minutes_ago=5), _row(notified="true", minutes_ago=3)]
    assert _recovery_map(rows)["rule-1"]["notified"] is True


def test_since_is_the_oldest_trigger_regardless_of_row_order():
    """'Active for' means the whole incident, not the latest re-raise."""
    newest = _row(minutes_ago=2)
    oldest = _row(minutes_ago=30)
    out = _recovery_map([newest, oldest])
    assert out["rule-1"]["since"] == oldest.triggered_at


def test_rows_without_a_rule_are_ignored():
    assert _recovery_map([_row(rule_id=None)]) == {}
