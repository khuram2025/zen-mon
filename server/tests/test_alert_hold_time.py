"""Hold time ("condition must exist for N seconds") on SNMP scalar rules.

The original implementation averaged the metric over the hold window, so a
single high sample could drag the mean past the threshold and fire early. With
~90s polling and a 300s hold, a device going 89% -> 100% breached the mean on
its first breaching poll. These pin the semantics the UI advertises: fire only
once the condition has been continuously true for the full duration.
"""
from types import SimpleNamespace

from app.services.network_alert_service import _eval_scalar_hold


def _rule(metric="memory", operator="gt", threshold=90, min_duration=300):
    return SimpleNamespace(metric=metric, operator=operator,
                           threshold=threshold, min_duration=min_duration)


def test_no_data_for_device_is_not_a_breach():
    assert _eval_scalar_hold(_rule(), None) is None
    assert _eval_scalar_hold(_rule(), {}) is None


def test_value_under_threshold_never_breaches():
    breach, value, _ = _eval_scalar_hold(_rule(), {"value": 40.0, "held_s": 9999})
    assert breach is False
    assert value == 40.0


def test_breaching_but_not_long_enough_does_not_fire():
    """The reported bug: firing before the hold time elapsed."""
    breach, _, _ = _eval_scalar_hold(_rule(min_duration=300), {"value": 100.0, "held_s": 90})
    assert breach is False


def test_breaching_for_the_full_hold_time_fires():
    breach, value, detail = _eval_scalar_hold(_rule(min_duration=300),
                                              {"value": 95.0, "held_s": 300})
    assert breach is True
    assert value == 95.0
    assert "for" in detail  # names how long it has been breaching


def test_hold_boundary_is_inclusive():
    assert _eval_scalar_hold(_rule(min_duration=300), {"value": 95.0, "held_s": 299})[0] is False
    assert _eval_scalar_hold(_rule(min_duration=300), {"value": 95.0, "held_s": 300})[0] is True


def test_zero_hold_fires_immediately():
    """The form says "0 = fire immediately"; averaging made it wait ~120s."""
    breach, _, detail = _eval_scalar_hold(_rule(min_duration=0), {"value": 95.0, "held_s": 0})
    assert breach is True
    # No duration clause when there is no hold time to report.
    assert "for" not in detail


def test_less_than_operator_holds_the_same_way():
    rule = _rule(operator="lt", threshold=10, min_duration=300)
    assert _eval_scalar_hold(rule, {"value": 5.0, "held_s": 60})[0] is False
    assert _eval_scalar_hold(rule, {"value": 5.0, "held_s": 600})[0] is True
    assert _eval_scalar_hold(rule, {"value": 50.0, "held_s": 600})[0] is False


def test_recovered_value_reports_not_breaching_so_open_alerts_resolve():
    """A cleared condition must return False, not None, or the alert never closes."""
    result = _eval_scalar_hold(_rule(), {"value": 10.0, "held_s": 0})
    assert result is not None
    assert result[0] is False
