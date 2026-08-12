"""APM alerting + SLO burn (AM-E6: F7/F8).

Unit tests cover the burn-rate math (hand-derived reference per doc 08's
SLO-burn-math acceptance), the t-digest CDF interpolation, and the APM rule
evaluator's raise/resolve flow against a scripted fake DB + fleet. API tests
cover the apm_* metric keys riding the generic alert-rules surface and SLO
CRUD validation/authorization.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import apm_alert_service, apm_slo_service
from app.services.apm_alert_service import (
    _cmp, _detail, _services_in_scope, evaluate_apm_rules, APM_PULL_METRICS,
)
from app.services.apm_slo_service import _frac_above, _Q_GRID, compute_slo_status, BURN_TIERS


# ── t-digest CDF interpolation ───────────────────────────────────────────────

_GRID_VALUES = [10, 20, 30, 50, 80, 100, 120, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000]


def test_frac_above_interpolates_between_quantiles():
    # 250ms falls midway between p70 (200ms) and p80 (300ms) -> ~25% above.
    assert _frac_above(250, _GRID_VALUES) == pytest.approx(0.25, abs=0.01)


def test_frac_above_edges():
    assert _frac_above(5, _GRID_VALUES) == 1.0        # faster than p1 -> all requests slower
    assert _frac_above(9999, _GRID_VALUES) == 0.0     # beyond p99.9 -> resolution floor
    assert _frac_above(100, []) == 0.0                # no data


def test_frac_above_flat_segment_snaps_to_upper_quantile():
    flat = [100.0] * len(_Q_GRID)
    # Every quantile is 100ms; threshold exactly 100 must not divide by zero.
    assert 0.0 <= _frac_above(100, flat) <= 1.0


# ── burn-rate math (hand-derived reference) ──────────────────────────────────

def _slo(sli_type="availability", target=99.9, window_days=30, threshold=None):
    return {
        "id": str(uuid4()), "name": "checkout availability",
        "service_name": "checkout", "env": "prod", "operation": None,
        "sli_type": sli_type, "latency_threshold_ms": threshold,
        "target": target, "window_days": window_days,
    }


def test_burn_rate_matches_hand_reference(monkeypatch):
    # target 99.9% -> budget 0.001. A steady 1.44% error rate burns at 14.4x.
    monkeypatch.setattr(apm_slo_service, "_bad_fraction",
                        lambda *a, **k: (0.0144, 10_000))
    st = compute_slo_status(_slo())
    fast = next(t for t in st["tiers"] if t["tier"] == "fast")
    assert fast["long_burn"] == pytest.approx(14.4, abs=0.01)
    assert fast["short_burn"] == pytest.approx(14.4, abs=0.01)
    assert fast["breaching"] is True          # 14.4 >= 14.4 on both windows
    mid = next(t for t in st["tiers"] if t["tier"] == "mid")
    assert mid["breaching"] is True           # 14.4 >= 6
    # Budget over the 30d window: 0.0144/0.001 = consumed 14.4x the whole budget.
    assert st["budget_consumed"] == pytest.approx(14.4, abs=0.01)
    assert st["budget_remaining"] == 0.0      # clamped


def test_burn_alert_clears_when_short_window_recovers(monkeypatch):
    # Incident recovered ~30min ago: the 5m/30m short windows are clean while
    # every longer window still carries the old badness. The page tiers (fast,
    # mid) must clear — their short window gates them — while the slow ticket
    # tier may legitimately stay open until its 6h short window drains.
    def frac(service, env, op, sli, thr, window_s):
        return (0.0, 500) if window_s <= 1_800 else (0.0288, 10_000)
    monkeypatch.setattr(apm_slo_service, "_bad_fraction", frac)
    st = compute_slo_status(_slo())
    by_tier = {t["tier"]: t for t in st["tiers"]}
    assert not by_tier["fast"]["breaching"]
    assert not by_tier["mid"]["breaching"]
    assert by_tier["slow"]["breaching"]  # ticket persists by design (6h short window)


def test_burn_no_data_windows_do_not_breach(monkeypatch):
    monkeypatch.setattr(apm_slo_service, "_bad_fraction", lambda *a, **k: None)
    st = compute_slo_status(_slo())
    assert all(not t["breaching"] for t in st["tiers"])
    assert st["budget_consumed"] is None


def test_tier_config_is_the_sre_workbook_canonical():
    assert [(t[0], t[1], t[2], t[3]) for t in BURN_TIERS] == [
        ("fast", 3_600, 300, 14.4),
        ("mid", 21_600, 1_800, 6.0),
        ("slow", 259_200, 21_600, 1.0),
    ]


# ── evaluator primitives ─────────────────────────────────────────────────────

def test_cmp_operators():
    assert _cmp(900, ">", 800) and not _cmp(700, ">", 800)
    assert _cmp(0.9, "lt", 0.95) and _cmp(5, ">=", 5)


def test_detail_formatting():
    assert _detail("apm_latency_p95", 812.3) == "p95 812ms"
    assert _detail("apm_error_rate", 0.023) == "error rate 2.30%"
    assert _detail("apm_throughput", 12.5) == "12.50 req/s"
    assert _detail("apm_apdex", 0.912) == "apdex 0.912"


def test_services_in_scope():
    fleet = {"checkout": {}, "Payments": {}}
    all_rule = SimpleNamespace(target=None)
    one_rule = SimpleNamespace(target="payments")   # case-insensitive exact
    miss_rule = SimpleNamespace(target="pay")       # substring must NOT match
    assert sorted(_services_in_scope(all_rule, fleet)) == ["Payments", "checkout"]
    assert _services_in_scope(one_rule, fleet) == ["Payments"]
    assert _services_in_scope(miss_rule, fleet) == []


# ── evaluator raise/resolve flow ─────────────────────────────────────────────

class _Result:
    def __init__(self, row=None, rows=None, rowcount=1):
        self._row, self._rows, self.rowcount = row, rows or [], rowcount

    def first(self):
        return self._row

    def all(self):
        return self._rows


class EvalFakeDB:
    """Scripted alert_rules + alerts store for evaluate_apm_rules."""

    def __init__(self, rules, active_alert_id=None, active_since=None):
        self.rules = rules
        self.active_alert_id = active_alert_id
        # The evaluator reads when the alert opened so its all-clear can say
        # how long the service was breaching.
        self.active_since = active_since or (
            datetime.now(timezone.utc) - timedelta(minutes=12))
        self.inserted, self.resolved = [], []

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if "FROM alert_rules" in sql:
            return _Result(rows=self.rules)
        if "SELECT id, triggered_at FROM alerts" in sql:
            row = ((self.active_alert_id, self.active_since)
                   if self.active_alert_id else None)
            return _Result(row=row)
        if "INSERT INTO alerts" in sql:
            self.inserted.append(params)
            return _Result()
        if "UPDATE alerts SET status = 'resolved'" in sql:
            self.resolved.append(params["id"])
            return _Result(rowcount=1)
        if "system_settings" in sql:
            return _Result(row=None)
        return _Result()

    async def commit(self):
        return None


def _rule(metric="apm_latency_p95", operator=">", threshold=800.0, target=None):
    return SimpleNamespace(
        id=uuid4(), name="High p95", metric=metric, operator=operator,
        threshold=threshold, severity="warning", min_duration=0,
        notify_channels=[], target=target, recovery_alert=True,
        conditions=None, condition_logic="AND",
        schedule_start=None, schedule_end=None, schedule_days=None,
    )


@pytest.mark.asyncio
async def test_evaluator_raises_on_breach(monkeypatch):
    fleet = {"checkout": {"apm_latency_p95": 900.0, "apm_latency_p50": 100.0,
                          "apm_latency_p99": 1200.0, "apm_error_rate": 0.0,
                          "apm_throughput": 5.0, "apm_apdex": 0.99, "reqs": 100}}
    monkeypatch.setattr(apm_alert_service, "_service_fleet", lambda w: fleet)
    db = EvalFakeDB([_rule()])
    out = await evaluate_apm_rules(db)
    assert out["raised"] == 1 and out["resolved"] == 0
    assert db.inserted and "checkout" in db.inserted[0]["msg"]


@pytest.mark.asyncio
async def test_evaluator_dedupes_existing_active_alert(monkeypatch):
    fleet = {"checkout": {"apm_latency_p95": 900.0, "apm_latency_p50": 0, "apm_latency_p99": 0,
                          "apm_error_rate": 0, "apm_throughput": 0, "apm_apdex": 1, "reqs": 100}}
    monkeypatch.setattr(apm_alert_service, "_service_fleet", lambda w: fleet)
    db = EvalFakeDB([_rule()], active_alert_id=uuid4())
    out = await evaluate_apm_rules(db)
    assert out["raised"] == 0 and not db.inserted


@pytest.mark.asyncio
async def test_evaluator_resolves_when_clear(monkeypatch):
    fleet = {"checkout": {"apm_latency_p95": 200.0, "apm_latency_p50": 0, "apm_latency_p99": 0,
                          "apm_error_rate": 0, "apm_throughput": 0, "apm_apdex": 1, "reqs": 100}}
    monkeypatch.setattr(apm_alert_service, "_service_fleet", lambda w: fleet)
    existing = uuid4()
    db = EvalFakeDB([_rule()], active_alert_id=existing)
    out = await evaluate_apm_rules(db)
    assert out["resolved"] == 1 and db.resolved == [existing]


@pytest.mark.asyncio
async def test_evaluator_scoped_rule_ignores_other_services(monkeypatch):
    fleet = {"inventory": {"apm_latency_p95": 9_999.0, "apm_latency_p50": 0, "apm_latency_p99": 0,
                           "apm_error_rate": 0, "apm_throughput": 0, "apm_apdex": 1, "reqs": 100}}
    monkeypatch.setattr(apm_alert_service, "_service_fleet", lambda w: fleet)
    db = EvalFakeDB([_rule(target="checkout")])
    out = await evaluate_apm_rules(db)
    assert out["raised"] == 0 and not db.inserted


# ── alert-rules API accepts apm_* keys ───────────────────────────────────────

def test_alert_rule_create_accepts_apm_metric(client, as_admin):
    resp = client.post("/api/v1/alert-rules", json={
        "name": "APM p95", "metric": "apm_latency_p95", "operator": ">",
        "threshold": 800, "target": "checkout",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metric"] == "apm_latency_p95"


def test_alert_rule_create_rejects_unknown_apm_metric(client, as_admin):
    resp = client.post("/api/v1/alert-rules", json={
        "name": "bogus", "metric": "apm_bogus", "operator": ">", "threshold": 1,
    })
    assert resp.status_code == 422


def test_all_nine_apm_keys_pass_schema_validation():
    from app.api.v1.alert_rules import _APM_METRICS
    keys = set(_APM_METRICS.split("|"))
    assert keys == {
        "apm_latency_p50", "apm_latency_p95", "apm_latency_p99",
        "apm_error_rate", "apm_throughput", "apm_apdex",
        "apm_slo_burn", "apm_synthetic_down", "apm_anomaly",
    }
    # The evaluator handles exactly the six pull-path RED keys.
    assert APM_PULL_METRICS < keys


# ── SLO API validation + authz ───────────────────────────────────────────────

def _slo_payload(**over):
    p = {"name": "Checkout availability", "service_name": "checkout", "env": "prod",
         "sli_type": "availability", "target": 99.9, "window_days": 30}
    p.update(over)
    return p


def test_slo_create_requires_latency_threshold_for_latency_sli(client, as_admin):
    resp = client.post("/api/v1/apm/slos", json=_slo_payload(sli_type="latency"))
    assert resp.status_code == 400
    assert "latency_threshold_ms" in resp.json()["detail"]


def test_slo_create_rejects_bad_window(client, as_admin):
    resp = client.post("/api/v1/apm/slos", json=_slo_payload(window_days=14))
    assert resp.status_code == 400


def test_slo_create_rejects_bad_target(client, as_admin):
    assert client.post("/api/v1/apm/slos", json=_slo_payload(target=100)).status_code == 422
    assert client.post("/api/v1/apm/slos", json=_slo_payload(target=0)).status_code == 422


def test_slo_write_requires_operator(client, as_viewer):
    resp = client.post("/api/v1/apm/slos", json=_slo_payload())
    assert resp.status_code == 403
