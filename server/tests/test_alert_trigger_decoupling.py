"""Down and degraded are separate trigger/recovery pairs.

The status engine used to treat degraded as down (`is_down = new_status in
("down", "degraded")`), so a 'Devices Down' rule paged on every latency blip:
Up -> Degraded fired it, Degraded -> Up reset it, and a link with jitter
produced a stream of DOWN pages for a device that never stopped answering.

`_trigger_matches` pins the decoupled semantics: each trigger_on owns exactly
its own state ('degraded' also owns the service checks' 'warning', its
degraded-class sibling), and 'any' still matches every transition.
"""

from app.api.v1.alert_engine import _trigger_matches


def test_down_rules_no_longer_fire_on_degraded():
    """The reported bug: Up -> Degraded paged the Devices-Down rule."""
    assert _trigger_matches("down", "degraded") is False


def test_down_rules_fire_on_down_only():
    assert _trigger_matches("down", "down") is True
    assert _trigger_matches("down", "up") is False
    assert _trigger_matches("down", "warning") is False


def test_degraded_owns_degraded_and_service_warning():
    assert _trigger_matches("degraded", "degraded") is True
    assert _trigger_matches("degraded", "warning") is True
    assert _trigger_matches("degraded", "down") is False
    assert _trigger_matches("degraded", "up") is False


def test_up_owns_up_only():
    assert _trigger_matches("up", "up") is True
    assert _trigger_matches("up", "down") is False
    assert _trigger_matches("up", "degraded") is False


def test_any_matches_every_transition():
    for status in ("up", "down", "degraded", "warning"):
        assert _trigger_matches("any", status) is True
