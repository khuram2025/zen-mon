from datetime import datetime, timezone
from app.services.device_status_explanation import measurement, degraded_reason

LIMITS = {"degraded_rtt_ms": 500, "degraded_loss_pct": 5}


def test_packet_loss_explains_event_without_blame_on_latency():
    sample = measurement((datetime.now(timezone.utc), .839, .5, 1), LIMITS)
    assert sample["packet_loss_pct"] == 50
    assert sample["breaches"] == [{"metric": "Packet loss", "value": 50.0, "threshold": 5,
                                   "exceeded_by": 45.0, "excess_unit": "percentage points"}]
    reason = degraded_reason(.839, .5, 500, 5)
    assert "50.00% exceeds 5% by 45.00 percentage points" in reason
    assert "Latency" not in reason


def test_limits_are_strict_and_independent():
    now = datetime.now(timezone.utc)
    assert not measurement((now, 500, .05, 1), LIMITS)["breaches"]
    # Float32 representation of exactly 5% must not produce a displayed breach.
    assert not measurement((now, 500, .05000000074505806, 1), LIMITS)["breaches"]
    sample = measurement((now, 501, .06, 1), LIMITS)
    assert {b["metric"] for b in sample["breaches"]} == {"Latency", "Packet loss"}
    assert "Latency 501.000 ms exceeds 500 ms by 1.000 ms" in degraded_reason(501, .06, 500, 5)


def test_down_and_missing_are_not_degraded():
    assert measurement(None, LIMITS) is None
    sample = measurement((datetime.now(timezone.utc), 0, 1, 0), LIMITS)
    assert not sample["is_up"]
    assert not sample["breaches"]


def test_historical_threshold_change_does_not_relabel_event(monkeypatch):
    from app.services import device_status_explanation as module
    from types import SimpleNamespace
    from datetime import timedelta
    at = datetime.now(timezone.utc) - timedelta(hours=2)
    responses = iter([
        [(at, .839, .5, 1)],
        [(at, "Recorded reason")],
        [(at, .839, .5, 1)],
        [(at + timedelta(seconds=59),)],
    ])
    class Client:
        def query(self, query, parameters):
            if "ping_metrics" in query:
                assert parameters["poller"] == "owner-sensor"
                assert "poller_id = {poller:String}" in query
            return SimpleNamespace(result_rows=next(responses))
    monkeypatch.setattr(module, "get_clickhouse_client", lambda: Client())
    result = module.evidence("00000000-0000-0000-0000-000000000001", "owner-sensor", LIMITS, at + timedelta(minutes=1), 60)
    assert result["latest"]["stale"]
    assert not result["recent_degraded"]["thresholds_unchanged"]
    assert result["recent_degraded"]["sample"]["breaches"] == []
    assert result["recent_degraded"]["reason"] == "Recorded reason"


def test_explanation_endpoint_preserves_device_visibility(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from app.api.v1 import devices
    async def get_device(*args):
        return SimpleNamespace(tags=["restricted"])
    async def scope(*args):
        return ["allowed"]
    monkeypatch.setattr(devices.device_service, "get_device", get_device)
    monkeypatch.setattr(devices.scoping, "visible_tags", scope)
    monkeypatch.setattr(devices.scoping, "entity_visible", lambda tags, allowed: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(devices.get_device_status_explanation("hidden", object(), object()))
    assert exc.value.status_code == 404


# ── canonical predicates, confirmation state and flapping (device_status_machine) ──

from datetime import timedelta
from types import SimpleNamespace

from app.services import device_status_explanation as explain
from app.services import device_status_machine as dsm

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
BAND = {"degraded_rtt_ms": 100, "degraded_loss_pct": 10, "degraded_rtt_clear_ms": 80, "degraded_loss_clear_pct": 8}


def test_degraded_reason_is_the_state_machines_text():
    assert degraded_reason is dsm.degraded_reason
    # fmt3 limits: 1234.5678 ms prints as 1234.568, not the 6-significant-digit :g form.
    assert "exceeds 1234.568 ms by" in degraded_reason(2000, 0, 1234.5678, 10)


def test_measurement_classifies_like_the_state_machine():
    def cls(rtt, loss, up=1):
        return measurement((NOW, rtt, loss, up), BAND)["classification"]
    assert cls(101, 0) == "breach"
    assert cls(95, 0) == "marginal"            # below the degraded limit, above the recovery limit
    assert cls(80, 0) == "clear"               # at the recovery limit is within it
    assert cls(10, 1 / 3) == "breach"          # one lost packet of three
    assert cls(0, 1, up=0) == "fail"
    # The canonical tolerance: representation noise at a limit is not a breach.
    assert not measurement((NOW, 100.0000005, 0, 1), BAND)["breaches"]
    assert measurement((NOW, 100.00001, 0, 1), BAND)["breaches"][0]["metric"] == "Latency"
    # Thresholds without recovery limits (older callers) still classify.
    assert measurement((NOW, 95, 0, 1), LIMITS)["classification"] == "clear"


def test_thresholds_and_confirmation_come_from_the_effective_policy():
    policy = dsm.policy_from_settings({"degraded_rtt_ms": 150, "down_confirm_checks": 4,
                                       "sensor_down_confirm_checks": 2, "up_confirm_checks": 3})
    assert explain.thresholds_for(policy) == {"degraded_rtt_ms": 150.0, "degraded_loss_pct": 10.0,
                                              "degraded_rtt_clear_ms": 120.0, "degraded_loss_clear_pct": 8.0}
    assert explain.confirmation_block(policy, "central", 30) == {
        "failed_checks": 4, "packets_per_check": 3, "interval_seconds": 30, "recovery_checks": 3}
    assert explain.confirmation_block(policy, "sensor", None)["failed_checks"] == 2
    assert explain.confirmation_block(policy, "sensor", None)["interval_seconds"] == 60


def _state(**kw):
    base = dict(last_sample_at=NOW - timedelta(seconds=20), status_since=NOW - timedelta(hours=1))
    base.update(kw)
    return dsm.State(**base)


def test_state_block_shows_each_streak_only_where_it_applies():
    policy = dsm.policy_from_settings({"up_confirm_checks": 2})
    down = explain.state_block(_state(hard_status="down", fail_streak=100, ok_streak=1), policy, "central", 60,
                               False, NOW)
    assert down["fail_streak"] is None and down["ok_streak"] == 1 and down["clear_streak"] is None
    assert down["pending"] == {"target": "up", "count": 1, "required": 2}

    # ok_streak lingers after down->up; two bad checks in the window are pending towards degraded.
    up = explain.state_block(_state(hard_status="up", ok_streak=2, window_bits=0b10100, window_len=5), policy,
                             "central", 60, False, NOW)
    assert up["ok_streak"] is None and up["fail_streak"] == 0 and up["clear_streak"] is None
    assert up["pending"] == {"target": "degraded", "count": 2, "required": 3}
    assert (up["bad_in_window"], up["window_len"], up["window_size"]) == (2, 5, 5)
    assert up["status_since"] == NOW - timedelta(hours=1) and up["stale"] is False

    degraded = explain.state_block(_state(hard_status="degraded", clear_streak=2), policy, "central", 60, False, NOW)
    assert degraded["clear_streak"] == 2
    assert degraded["pending"] == {"target": "up", "count": 2, "required": 3}

    # A failure streak outranks window evidence; the sensor's own down confirmation applies.
    failing = explain.state_block(_state(hard_status="up", fail_streak=1, window_bits=0b1, window_len=1), policy,
                                  "sensor", 60, False, NOW)
    assert failing["pending"] == {"target": "down", "count": 1, "required": policy.sensor_down_confirm_checks}


def test_state_block_drops_pending_progress_after_the_gap_rule_would_reset_it():
    policy = dsm.Policy()
    exact = _state(hard_status="up", fail_streak=2, last_sample_at=NOW - timedelta(seconds=180))
    assert explain.state_block(exact, policy, "central", 60, False, NOW)["pending"]["target"] == "down"
    silent = _state(hard_status="up", fail_streak=2, last_sample_at=NOW - timedelta(seconds=181))
    block = explain.state_block(silent, policy, "central", 60, False, NOW)
    assert block["stale"] is True and block["pending"] is None and block["fail_streak"] == 2
    # Long intervals stretch the gap: 3 x 600 s.
    assert explain.state_block(silent, policy, "central", 600, False, NOW)["stale"] is False


def test_state_block_freshness_takes_the_newest_known_check():
    policy = dsm.Policy()
    heartbeat = _state(hard_status="up", last_sample_at=NOW - timedelta(minutes=9))
    fresh = explain.state_block(heartbeat, policy, "central", 60, False, NOW,
                                last_check_at=(NOW - timedelta(seconds=30)).replace(tzinfo=None))
    assert fresh["stale"] is False and fresh["last_sample_at"] == NOW - timedelta(seconds=30)
    # An older check time never makes the row look older.
    older = explain.state_block(_state(hard_status="up"), policy, "central", 60, False, NOW,
                                last_check_at=NOW - timedelta(hours=1))
    assert older["last_sample_at"] == NOW - timedelta(seconds=20) and older["stale"] is False
    assert explain.newest(None, None) is None


def test_current_state_reseeds_rows_the_writer_would_not_trust():
    since = NOW - timedelta(days=2)
    row = {**dsm.state_to_row(_state(hard_status="down", fail_streak=9, owner="central")), "device_id": "x"}
    state, seeded = explain.current_state(row, "central", "down", since)
    assert not seeded and state.fail_streak == 9
    # Another owner's row, a status changed behind the row, or no row: a silent seed.
    for owner, status, r in (("sensor-uuid", "down", row), ("central", "unknown", row), ("central", "up", None)):
        state, seeded = explain.current_state(r, owner, status, since)
        assert seeded and state.fail_streak == 0 and state.owner == owner
        assert state.hard_status == (status if status in ("up", "down") else "unknown")
        assert state.status_since == since
    state, seeded = explain.current_state(None, "central", "maintenance", None)
    assert seeded and state.in_maintenance and state.hard_status == "unknown"


def test_max_detectable_interval_follows_the_confirmation_counts():
    defaults = dsm.Policy()
    # Reachability: 4 changes alternate 3 failed / 1 answered checks -> 5 checks inside 30 min.
    assert explain.max_detectable_interval_s(defaults, "central") == 359
    # Sensors confirm down on one check: 3 checks inside 30 min.
    assert explain.max_detectable_interval_s(defaults, "sensor") == 599
    # Single-check legacy behaviour: 3 checks.
    legacy = dsm.policy_from_settings(dsm.legacy_preset({}))
    assert explain.max_detectable_interval_s(legacy, "central") == 599
    # The faster class decides: performance (2 + 2 + 2 = 6 checks) beats reachability (10 + 1 + 1 = 12).
    slow_down = dsm.policy_from_settings({"down_confirm_checks": 10, "degraded_confirm_checks": 2,
                                          "degraded_clear_checks": 2, "degraded_window_checks": 2})
    assert explain.max_detectable_interval_s(slow_down, "central") == 299


def test_earliest_stop_is_the_sooner_of_stability_and_count_per_class():
    policy = dsm.Policy()       # window 30, stop at <= 1 change, stable 10
    t = lambda m: NOW + timedelta(minutes=m)  # noqa: E731
    burst = _state(reach_flapping=True, reach_changes=[t(-20), t(-15), t(-10), t(-2)])
    assert explain.earliest_stop_at(burst, policy) == t(8)              # stability: last change + 10
    spread = _state(perf_flapping=True, perf_changes=[t(-29), t(-28), t(-27), t(-1)])
    assert explain.earliest_stop_at(spread, policy) == t(3)             # count: 3rd change ages out at -27 + 30
    both = _state(reach_flapping=True, perf_flapping=True, reach_changes=burst.reach_changes,
                  perf_changes=spread.perf_changes)
    assert explain.earliest_stop_at(both, policy) == t(8)               # the device stops with its last class
    assert explain.earliest_stop_at(_state(), policy) is None
    off = dsm.policy_from_settings({"flap_detection_enabled": False})
    assert explain.earliest_stop_at(burst, off) is None


def test_flapping_block_reports_flags_counts_and_detectability():
    policy = dsm.Policy()
    t = lambda m: NOW + timedelta(minutes=m)  # noqa: E731
    state = _state(reach_flapping=True, reach_changes=[t(-45), t(-20), t(-15), t(-10), t(-2)],
                   perf_changes=[t(-31), t(-5)])
    device = SimpleNamespace(is_flapping=True, flap_kind="reachability", flapping_since=t(-10))
    block = explain.flapping_block(device, state, policy, "central", 60, NOW)
    assert block == {
        "active": True, "kind": "reachability", "since": t(-10),
        "reach_changes_in_window": 4, "perf_changes_in_window": 1,
        "window_minutes": 30, "start_changes": 4, "stop_changes": 1, "stable_minutes": 10, "enabled": True,
        "earliest_stop_at": t(8), "max_detectable_interval_s": 359, "detectable": True,
    }
    calm = explain.flapping_block(SimpleNamespace(is_flapping=False, flap_kind=None, flapping_since=None),
                                  dsm.State(), policy, "central", 3600, NOW)
    assert calm["active"] is False and calm["kind"] is None and calm["earliest_stop_at"] is None
    # At a one-hour interval four confirmed changes can never fit in 30 minutes.
    assert calm["detectable"] is False


def test_recent_degraded_sample_keeps_no_classification_after_a_threshold_change(monkeypatch):
    from app.services import device_status_explanation as module
    at = datetime.now(timezone.utc) - timedelta(hours=2)
    responses = iter([[], [(at, "Latency 150.000 ms exceeds 100 ms by 50.000 ms")], [(at, 150.0, 0.0, 1)], []])

    class Client:
        def query(self, query, parameters):
            return SimpleNamespace(result_rows=next(responses))
    monkeypatch.setattr(module, "get_clickhouse_client", lambda: Client())
    result = module.evidence("00000000-0000-0000-0000-000000000001", "poller-01", BAND,
                             at + timedelta(minutes=5), 60)
    assert result["latest"] is None
    assert result["recent_degraded"]["sample"]["breaches"] == []
    assert result["recent_degraded"]["sample"]["classification"] is None
    assert result["recent_degraded"]["recovered_at"] is None
