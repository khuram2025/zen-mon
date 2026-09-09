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
