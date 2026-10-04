"""NetFlow capacity (B4): NF-13..NF-16, CR-08, CR-09, FE-05, X-05, X-26, X-27, X-36.

Layers:
  * unit: the statistical contract (nearest rank, minimum samples, complete
    slots, slot selection, headroom, states, forecast, settings validation,
    direction accounting, speed rule and resolution states) -- always run;
  * in-process HTTP over a fake ClickHouse (the real /netflow router): shapes,
    validation, full-set summaries and risk ranking, CSV, permissions and audit
    of the write endpoints -- always run;
  * scratch ClickHouse (NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py):
    the review's capacity fixtures through the real SQL -- partial edges, idle
    zero-fill, collection gaps, nearest-rank determinism, duration spreading,
    the volume guard, duplex, hairpin and duplicate-direction export, adjacent
    windows, the raw fallback, SNMP/sFlow cross-checks and the trend forecast;
  * live API (NETFLOW_TEST_API, own uvicorn of this tree, e.g. :8114).
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.api.v1.netflow as netflow  # noqa: E402
from app.api.v1.netflow import access, capacity as CAP, common as C  # noqa: E402
from app.api.v1.netflow.access import ExporterOwner, FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import Window  # noqa: E402
from app.api.v1.netflow.filters import FlowFilter, IfaceRef  # noqa: E402
from app.api.v1.netflow.sql import floor_to  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_capacity as cap  # noqa: E402
from app.services import netflow_classify, netflow_groups  # noqa: E402

UTC = timezone.utc
ADMIN_VIS = FlowVisibility(restricted=False, exporters=None, user_id=None,
                           permissions=frozenset({"system.admin"}))
VIEW_VIS = FlowVisibility(restricted=False, exporters=None, user_id="v", permissions=frozenset({"netflow.view"}))
SETTINGS = cap.DEFAULT_SETTINGS


# ── Nearest rank and minimum samples ─────────────────────────────────────────

def test_nearest_rank_discards_the_top_share():
    assert cap.nearest_rank(12, 95) == 12          # nothing discarded: the maximum
    assert cap.nearest_rank(20, 95) == 19          # the 19th of 20
    assert cap.nearest_rank(8640, 95) == 8208      # a 30-day month of 5-minute samples
    assert cap.nearest_rank(4000, 95) == 3800
    assert cap.nearest_rank(10, 90) == 9
    assert cap.nearest_rank(0, 95) == 0
    for n in range(1, 500):
        for p in cap.PERCENTILES:
            # rank = ceil(n * p / 100), computed without floating point
            assert cap.nearest_rank(n, p) == -(-(n * p) // 100)


def test_min_samples_is_where_a_sample_is_discarded():
    assert {p: cap.min_samples(p) for p in cap.PERCENTILES} == {95: 20, 90: 10, 85: 7, 80: 5, 75: 4}
    for p in cap.PERCENTILES:
        n = cap.min_samples(p)
        assert cap.nearest_rank(n, p) == n - 1 and cap.nearest_rank(n - 1, p) == n - 1


def test_percentile_zero_padding_equals_materialised_zeros():
    rng = random.Random(7)
    for _ in range(200):
        vals = sorted(rng.uniform(0.1, 100) for _ in range(rng.randint(0, 60)))
        zeros = rng.randint(0, 80)
        full = sorted([0.0] * zeros + vals)
        for p in cap.PERCENTILES:
            got = cap.percentile_value(vals, p, zeros)
            if not full:
                assert got is None
            else:
                assert got == full[cap.nearest_rank(len(full), p) - 1]


def test_series_stats_sparse_week_is_not_the_busy_rate():
    # F4: 10 busy 5-minute slots at 100 Mbps in a live 7-day window.
    s = cap.series_stats([100e6] * 10, zeros=2006, pct=95)
    assert s["samples"] == 2016 and round(s["avg_bps"]) == 496032
    assert s["p_bps"] == 0.0 and s["max_bps"] == 100e6


def test_p_available_reasons():
    assert cap.p_available(0, None, 95, 80) == "no_samples"
    assert cap.p_available(12, 100.0, 95, 80) == "too_few_samples"
    assert cap.p_available(20, 100.0, 95, 80) is None
    assert cap.p_available(200, 79.9, 95, 80) == "low_coverage"
    assert cap.p_available(10, 100.0, 90, 80) is None


# ── Complete slots and slot selection ─────────────────────────────────────────

def test_complete_slots_exclude_partial_edges():
    sw = cap.complete_slots(datetime(2026, 9, 1, 11, 8, 51, tzinfo=UTC), datetime(2026, 9, 1, 12, 8, 51, tzinfo=UTC))
    assert sw.first == datetime(2026, 9, 1, 11, 10, tzinfo=UTC) and sw.end == datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    assert sw.n_slots == 11 and sw.head_excluded_s == 69 and sw.tail_excluded_s == 231
    aligned = cap.complete_slots(datetime(2026, 9, 1, 11, tzinfo=UTC), datetime(2026, 9, 1, 12, tzinfo=UTC))
    assert aligned.n_slots == 12 and aligned.head_excluded_s == 0 and aligned.tail_excluded_s == 0
    tiny = cap.complete_slots(datetime(2026, 9, 1, 11, 1, tzinfo=UTC), datetime(2026, 9, 1, 11, 4, tzinfo=UTC))
    assert tiny.empty and tiny.n_slots == 0
    cut = cap.complete_slots(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC),
                             horizon=datetime(2025, 8, 27, 7, 3, tzinfo=UTC))
    assert cut.retention_cut and cut.first == datetime(2025, 8, 27, 7, 5, tzinfo=UTC)
    assert cut.shifted_back().end == cut.first


def test_business_hours_select_local_slots():
    bh = cap.BusinessHours(enabled=True, start_hour=8, end_hour=18, days=(1, 2, 3, 4, 5), tz="Asia/Riyadh")
    settings = cap.CapacitySettings(business_hours=bh)
    sel = cap.build_selector(None, settings, business_hours=True)
    # Monday 2026-09-28 00:00 Riyadh = Sunday 21:00 UTC; one full local week.
    start = datetime(2026, 9, 27, 21, tzinfo=UTC)
    sw = cap.complete_slots(start, start + timedelta(days=7))
    assert cap.expected_slots(sw, sel) == 5 * 10 * 12
    assert sel.matches(datetime(2026, 9, 28, 5, 0, tzinfo=UTC))       # Mon 08:00 local
    assert not sel.matches(datetime(2026, 9, 28, 4, 55, tzinfo=UTC))  # Mon 07:55 local
    assert sel.matches(datetime(2026, 10, 2, 6, tzinfo=UTC))           # Fri 09:00 local (ISO day 5)
    assert not sel.matches(datetime(2026, 10, 3, 6, tzinfo=UTC))       # Sat 09:00 local
    wrap = cap.BusinessHours(enabled=True, start_hour=22, end_hour=6, days=(1, 2, 3, 4, 5, 6, 7))
    assert wrap.hours() == frozenset({22, 23, 0, 1, 2, 3, 4, 5})
    # The page's hour filter is a slot rule too (evaluated in its own zone).
    f = FlowFilter(hour=9, tz="UTC")
    sel2 = cap.build_selector(f, SETTINGS, business_hours=False)
    assert cap.expected_slots(cap.complete_slots(start, start + timedelta(days=1)), sel2) == 12
    params: dict = {}
    sql = sel2.sql("slot", params)
    assert "toHour(slot" in sql and params["cap_sel0_h"] == (9,)


# ── Utilization, headroom, states (NF-15) ─────────────────────────────────────

def test_headroom_and_states():
    assert cap.headroom(2e8, 10**9) == (8e8, 80.0)          # 20 % used -> 80 % free
    assert cap.headroom(0.0, 10**9) == (1e9, 100.0)
    assert cap.headroom(1.2e9, 10**9) == (-2e8, -20.0)      # over: real value, negative room
    assert cap.headroom(5e8, None) == (None, None)          # unknown speed: unknown
    assert cap.classify(None, SETTINGS) == "unknown"
    assert cap.classify(None, SETTINGS, has_samples=False) == "insufficient"
    assert cap.classify(120.0, SETTINGS) == "over"
    assert cap.classify(95.0, SETTINGS) == "critical"
    assert cap.classify(75.0, SETTINGS) == "warning"
    assert cap.classify(20.0, SETTINGS) == "healthy"


def _stats(**kw):
    base = dict(exporter="10.0.0.1", ifindex=7, n_traffic=24, n_live=24, samples=24, zeros=0, rnk=23,
                in_p=None, in_max=None, in_sum=0.0, out_p=None, out_max=None, out_sum=0.0,
                sum_p=None, sum_max=None, sum_sum=0.0, in_bytes=0.0, out_bytes=0.0, in_packets=0.0,
                out_packets=0.0, flows=0, last_slot=None)
    base.update(kw)
    return base


def _meta(**kw):
    m = cap.IfaceMeta(exporter=kw.pop("exporter", "10.0.0.1"), ifindex=kw.pop("ifindex", 7))
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def test_duplex_600_each_way_is_60_percent_not_120():
    # NF-14 acceptance: 1 Gbps full duplex carrying 600 Mbps each way.
    stats = _stats(in_p=6e8, in_max=6e8, in_sum=6e8 * 24, out_p=6e8, out_max=6e8, out_sum=6e8 * 24,
                   sum_p=1.2e9, sum_max=1.2e9, sum_sum=1.2e9 * 24)
    meta = _meta(device_id="d1", device_interfaces=4, found=True, snmp_speed=10**9)
    row = cap.build_row(stats, meta, 24, SETTINGS)
    assert row["in"]["utilization_p_pct"] == 60.0 and row["out"]["utilization_p_pct"] == 60.0
    assert row["headline"]["utilization_pct"] == 60.0 and row["headline"]["state"] == "healthy"
    assert row["combined"]["p_bps"] == 1.2e9
    assert not any("pct" in k for k in row["combined"]), "combined throughput must never be a percentage"
    assert row["headline"]["headroom_bps"] == 4e8 and row["headline"]["headroom_pct"] == 40.0
    assert row["billable"]["bps"] == 6e8 and row["billable"]["mode"] == "separate"
    merged = cap.build_row(stats, meta, 24, cap.CapacitySettings(direction_mode="merged"))
    assert merged["billable"] == {"bps": 1.2e9, "direction": "merged", "mode": "merged"}


def test_over_capacity_keeps_its_real_value_and_explains():
    stats = _stats(in_p=1.2e9, in_max=1.3e9, in_sum=1.2e9 * 24, out_p=1e6, out_max=1e6, out_sum=1e6 * 24)
    row = cap.build_row(stats, _meta(device_id="d1", device_interfaces=1, found=True, snmp_speed=10**9), 24, SETTINGS)
    h = row["headline"]
    assert h["state"] == "over" and h["utilization_pct"] == 120.0 and h["direction"] == "in"
    assert h["headroom_pct"] == -20.0 and "exceeds" in h["note"]


def test_unknown_speed_is_unknown_never_healthy():
    stats = _stats(in_p=1e6, in_max=2e6, in_sum=24e6, out_p=1e5, out_max=1e5, out_sum=24e5)
    row = cap.build_row(stats, _meta(), 24, SETTINGS)
    assert row["headline"]["state"] == "unknown" and row["headline"]["utilization_pct"] is None
    assert row["in"]["utilization_p_pct"] is None and row["headline"]["headroom_pct"] is None
    assert row["resolution"] == "exporter_unmapped"


def test_too_few_samples_shows_the_peak_labelled():
    stats = _stats(samples=12, n_traffic=12, n_live=12, in_p=11e6, in_max=12e6, in_sum=78e6)
    row = cap.build_row(stats, _meta(device_id="d", device_interfaces=1, found=True, snmp_speed=10**8), 12, SETTINGS)
    assert row["in"]["p_bps"] is None and row["in"]["p_reason"] == "too_few_samples"
    assert row["headline"]["statistic"] == "max" and row["headline"]["value_bps"] == 12e6
    assert row["headline"]["reason"] == "too_few_samples"
    assert row["billable"]["bps"] is None
    # Complete coverage with few samples (a short window): the peak is the whole story.
    assert row["headline"]["state"] == "healthy"


def test_low_coverage_peak_never_vouches_for_health():
    meta = _meta(device_id="d", device_interfaces=1, found=True, snmp_speed=10**8)
    # 30 of 2016 expected samples: a peak of 12 % says nothing about the gaps.
    quiet = _stats(samples=30, n_traffic=30, n_live=30, in_p=11e6, in_max=12e6, in_sum=300e6)
    row = cap.build_row(quiet, meta, 2016, SETTINGS)
    assert row["headline"]["reason"] == "low_coverage" and row["headline"]["statistic"] == "max"
    assert row["headline"]["state"] == "insufficient" and row["headline"]["utilization_pct"] == 12.0
    # ...but a peak above a threshold is a fact the gaps cannot undo.
    busy = _stats(samples=30, n_traffic=30, n_live=30, in_p=8e7, in_max=9.5e7, in_sum=2e9)
    assert cap.build_row(busy, meta, 2016, SETTINGS)["headline"]["state"] == "critical"
    over = _stats(samples=30, n_traffic=30, n_live=30, in_p=8e7, in_max=1.5e8, in_sum=2e9)
    assert cap.build_row(over, meta, 2016, SETTINGS)["headline"]["state"] == "over"
    # A short range that is also poorly covered (7 of 11 slots): too few samples AND low coverage.
    short = _stats(samples=7, n_traffic=7, n_live=7, in_p=3e7, in_max=3e7, in_sum=2e8)
    row = cap.build_row(short, meta, 11, SETTINGS)
    assert row["headline"]["reason"] == "too_few_samples" and row["headline"]["state"] == "insufficient"


# ── Speed rule and resolution states (NF-16, X-05) ────────────────────────────

def test_speed_rule_configured_then_override_then_snmp_then_sflow():
    m = _meta(device_id="d", device_interfaces=3, found=True, snmp_speed=10**9, configured_speed=5 * 10**7)
    assert m.speed("in") == (5 * 10**7, cap.SOURCE_CONFIGURED)          # circuit beats SNMP
    m = _meta(device_id="d", device_interfaces=3, found=True, snmp_speed=None, configured_speed=10**8)
    assert m.speed("out") == (10**8, cap.SOURCE_CONFIGURED)             # override-only interface
    m = _meta(override={"speed_in_bps": 2 * 10**7, "speed_out_bps": 10**7}, snmp_speed=10**9)
    assert m.speed("in") == (2 * 10**7, cap.SOURCE_OVERRIDE) and m.speed("out") == (10**7, cap.SOURCE_OVERRIDE)
    m = _meta(sflow_speed=10**10)
    assert m.speed("in") == (10**10, cap.SOURCE_SFLOW)
    assert _meta(snmp_speed=0).speed("in") == (None, cap.SOURCE_UNKNOWN)


def test_resolution_states():
    assert _meta().resolution == "exporter_unmapped"
    assert _meta(device_id="d", device_interfaces=0).resolution == "no_interface_inventory"
    assert _meta(device_id="d", device_interfaces=5, found=False).resolution == "interface_not_found"
    assert _meta(device_id="d", device_interfaces=5, found=True).resolution == "speed_unknown"
    assert _meta(device_id="d", device_interfaces=5, found=True, snmp_speed=10**9).resolution == "resolved"
    # A NetFlow override resolves even an unmapped exporter.
    assert _meta(override={"speed_in_bps": 10**8, "speed_out_bps": 10**8}).resolution == "resolved"
    assert _meta(override={"display_name": "WAN-1"}).display_name == "WAN-1"


# ── Direction accounting (NF-14, FE-12) ───────────────────────────────────────

def test_accounting_basis_and_dedup_sql():
    assert cap.accounting_basis("auto", [0, 1]) == "ingress_records"
    assert cap.accounting_basis("auto", [1]) == "all_records"
    assert cap.accounting_basis("auto", [255]) == "all_records"
    assert cap.accounting_basis("egress", [0, 1]) == "egress_records"
    assert cap.accounting_basis("both", [0, 1]) == "all_records"
    params: dict = {}
    sql = cap.dedup_sql(cap.DirectionPolicy(), params)
    assert "flow_direction = 1" in sql and "nf_cap_auto" in sql and not params
    pol = cap.DirectionPolicy(ingress=("::ffff:10.0.0.1",), egress=("::ffff:10.0.0.2",), both=("::ffff:10.0.0.3",))
    sql = cap.dedup_sql(pol, params)
    assert params["cap_dir_explicit"] == ("::ffff:10.0.0.1", "::ffff:10.0.0.2", "::ffff:10.0.0.3")
    assert "flow_direction = 0" in sql and params["cap_dir_egress"] == ("::ffff:10.0.0.2",)


# ── Forecast (CR-09) ──────────────────────────────────────────────────────────

def _series(n, start_bps, growth, today=date(2026, 9, 30)):
    return [(today - timedelta(days=n - i), start_bps + growth * i) for i in range(n)]


def test_forecast_needs_fourteen_days():
    fc = cap.forecast(_series(13, 1e8, 5e6), 10**9, SETTINGS, date(2026, 9, 30))
    assert fc["status"] == "insufficient_history" and fc["days_to_full"] is None and "14" in fc["reason"]


def test_forecast_linear_growth_days_to_levels():
    pts = _series(30, 1e8, 1e7)            # +10 Mbps per day; last point (yesterday) = 390 Mbps
    fc = cap.forecast(pts, 10**9, SETTINGS, date(2026, 9, 30))
    assert fc["status"] == "ok" and fc["confidence"] == "high" and fc["r2"] == 1.0
    assert fc["slope_bps_per_day"] == 1e7 and fc["fitted_now_bps"] == 4e8
    assert fc["days_to_warn"] == 30.0 and fc["days_to_crit"] == 50.0 and fc["days_to_full"] == 60.0


def test_forecast_flat_unknown_speed_and_horizon():
    assert cap.forecast(_series(20, 1e8, 0), 10**9, SETTINGS, date(2026, 9, 30))["status"] == "not_growing"
    assert cap.forecast(_series(20, 1e8, 1e6), None, SETTINGS, date(2026, 9, 30))["status"] == "speed_unknown"
    slow = cap.forecast(_series(20, 1e6, 1.0), 10**9, SETTINGS, date(2026, 9, 30))
    assert slow["status"] == "beyond_horizon" and slow["days_to_full"] is None
    already = cap.forecast(_series(20, 9.5e8, 1e6), 10**9, SETTINGS, date(2026, 9, 30))
    assert already["days_to_warn"] == 0.0 and already["days_to_crit"] == 0.0
    noisy = [(d, v * (1.5 if i % 2 else 0.5)) for i, (d, v) in enumerate(_series(20, 1e8, 1e6))]
    assert cap.forecast(noisy, 10**9, SETTINGS, date(2026, 9, 30))["confidence"] == "low"


# ── Settings (CR-08) ──────────────────────────────────────────────────────────

def test_settings_validation():
    s = cap.parse_settings({"percentile": 90, "direction_mode": "merged", "warn_pct": 60, "crit_pct": 85,
                            "business_hours": {"enabled": True, "start_hour": 7, "end_hour": 19,
                                               "days": [5, 1, 2], "tz": "Asia/Riyadh"}})
    assert s.percentile == 90 and s.direction_mode == "merged" and s.business_hours.days == (1, 2, 5)
    with pytest.raises(cap.SettingsError) as e:
        cap.parse_settings({"percentile": 99, "direction_mode": "sum", "warn_pct": 95, "crit_pct": 90,
                            "business_hours": {"days": [], "tz": "Mars/Olympus"}, "bogus": 1})
    assert set(e.value.fields) >= {"percentile", "direction_mode", "warn_pct", "business_hours.days",
                                   "business_hours.tz", "bogus"}
    with pytest.raises(cap.SettingsError):
        cap.parse_settings({"percentile": True})
    lenient = cap.parse_settings({"percentile": 99, "warn_pct": 50}, strict=False)
    assert lenient.percentile == 95 and lenient.warn_pct == 50
    assert cap.parse_settings({}) == cap.DEFAULT_SETTINGS


def test_month_bounds_and_csv_safety():
    s, e = cap.month_bounds("2026-02", "Asia/Riyadh")
    assert s == datetime(2026, 1, 31, 21, tzinfo=UTC) and e == datetime(2026, 2, 28, 21, tzinfo=UTC)
    assert cap.month_bounds("2026-12", "UTC")[1] == datetime(2027, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        cap.month_bounds("2026-13", "UTC")
    assert cap.csv_safe("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0" and cap.csv_safe("wan-1") == "wan-1"
    body = cap.to_csv(["a", "b"], [["=1+1", 2.5], [None, "x"]])
    assert body.splitlines() == ["a,b", "'=1+1,2.5", ",x"]


# ── HTTP over a fake ClickHouse ───────────────────────────────────────────────

TTL_ROWS = [
    ("flow_records", "MergeTree TTL toDateTime(timestamp) + toIntervalDay(30)"),
    ("flow_conversations_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_qos_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_interface_5m", "AggregatingMergeTree TTL timestamp + toIntervalDay(400)"),
    ("flow_exporter_minutes", "AggregatingMergeTree TTL minute + toIntervalDay(400)"),
]


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self, stats_rows=None):
        self.calls: list[tuple[str, dict]] = []
        self.stats_rows = stats_rows or []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}))
        if "system.tables" in sql and "count()" in sql:
            return FakeResult([(0,)])
        if "system.tables" in sql:
            return FakeResult(TTL_ROWS)
        if "system.parts" in sql:
            return FakeResult([])
        if "n_traffic" in sql and "capacity" not in sql and "rnk" in sql and "toDate(" not in sql:
            return FakeResult(self.stats_rows)
        if "GROUP BY" in sql or "ORDER BY" in sql:
            return FakeResult([])
        return FakeResult([(None, None)])

    def command(self, sql, parameters=None):
        return None


class FakeDB:
    """PostgreSQL stand-in: answers by SQL substring, records writes."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.writes: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self.writes.append((sql, params or {}))
        rows = []
        for needle, value in self.answers.items():
            if needle in sql:
                rows = value
                break
        return SimpleNamespace(all=lambda: list(rows), first=lambda: rows[0] if rows else None,
                               scalar=lambda: None)

    def begin_nested(self):
        db = self

        class _Nested:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Nested()

    async def commit(self):
        self.commits += 1


VIEWER = SimpleNamespace(id=uuid4(), username="viewer", role="viewer", is_active=True, scope_tags=[])
MANAGER = SimpleNamespace(id=uuid4(), username="netops", role="operator", is_active=True, scope_tags=[])


def make_client(vis, db, user=VIEWER, manage=False):
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield db

    async def get_user():
        return user

    async def visibility():
        return vis

    async def manager():
        if not manage:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = get_user
    app.dependency_overrides[access.require_view] = get_user
    app.dependency_overrides[access.require_manage] = manager
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


@pytest.fixture
def fake_env(monkeypatch):
    async def no_refresh(*a, **kw):
        return None

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    C._retention_cache.update(value=None, at=0.0)
    yield monkeypatch
    C._retention_cache.update(value=None, at=0.0)


URL = "/api/v1/netflow"
WAN_DEVICE = "11111111-1111-1111-1111-111111111111"


def _fleet_rows():
    """60 busy 10 Gbps LAN interfaces (~2-4 Gbps) and one saturated 10 Mbps WAN."""
    rows = []
    for i in range(60):
        v = 2e9 + i * 3e7
        rows.append(("10.9.0.1", 100 + i, 288, 288, 288, 0, 274, v, v * 1.2, v * 288, v / 2, v / 2 * 1.1,
                     v / 2 * 288, v * 1.5, v * 1.8, v * 1.5 * 288, v * 1e3, v * 5e2, 1e6, 1e6, 100,
                     datetime(2026, 9, 30, 11, 55)))
    v = 9.6e6
    rows.append(("10.9.0.1", 1, 288, 288, 288, 0, 274, v, 9.9e6, v * 288, 1e6, 1.2e6, 1e6 * 288,
                 1.06e7, 1.1e7, 1.06e7 * 288, 1e9, 1e8, 1e5, 1e5, 10, datetime(2026, 9, 30, 11, 55)))
    return rows


def _fleet_db():
    inventory = [(WAN_DEVICE, 1, "Gi0/0", "WAN uplink", "ISP-A 10M", 10**9, 10**7)]
    inventory += [(WAN_DEVICE, 100 + i, f"Te1/0/{i}", None, None, 10**10, None) for i in range(60)]
    return FakeDB({"FROM device_interfaces": inventory})


@pytest.fixture
def fleet(fake_env):
    ch = FakeCH(_fleet_rows())
    fake_env.setattr(C, "get_ch_client", lambda: ch)

    async def owners(db, ips=None):
        return {"10.9.0.1": ExporterOwner(exporter="10.9.0.1", device_id=WAN_DEVICE, hostname="edge-01",
                                          source="mapped")}

    fake_env.setattr(cap, "resolve_exporter_owners", owners)
    return ch


def test_small_saturated_wan_ranks_first_and_counts_over_the_whole_set(fleet):
    # X-27: ranking by absolute bps with LIMIT dropped small saturated circuits.
    client = make_client(ADMIN_VIS, _fleet_db())
    body = client.get(URL + "/capacity", params={"hours": 24, "envelope": 1, "limit": 5}).json()
    data = body["data"]
    assert data["items"][0]["ifindex"] == 1 and data["items"][0]["display_name"] == "Gi0/0"
    wan = data["items"][0]
    assert wan["speed"]["in_bps"] == 10**7 and wan["speed"]["source_in"] == "configured"   # X-05
    assert wan["headline"]["state"] == "critical" and wan["headline"]["utilization_pct"] == 96.0
    s = data["summary"]
    assert s["interfaces_total"] == 61 and s["speed_known"] == 61 and len(data["items"]) == 5
    assert s["by_state"]["critical"] == 1 and s["by_state"]["healthy"] + s["by_state"]["warning"] == 60
    assert data["matched"] == 61
    # Paging keeps the order and the full-set summary.
    page2 = client.get(URL + "/capacity", params={"hours": 24, "envelope": 1, "limit": 5, "offset": 5}).json()["data"]
    assert page2["summary"] == s and page2["items"][0]["key"] != data["items"][0]["key"]
    # Search, state filter and sort.
    only = client.get(URL + "/capacity", params={"hours": 24, "envelope": 1, "q": "isp-a"}).json()["data"]
    assert [r["ifindex"] for r in only["items"]] == [1] and only["summary"]["interfaces_total"] == 61
    crit = client.get(URL + "/capacity", params={"hours": 24, "envelope": 1, "state": "critical"}).json()["data"]
    assert crit["matched"] == 1
    by_name = client.get(URL + "/capacity", params={"hours": 24, "v": 2, "sort": "name", "order": "asc"}).json()
    assert by_name["items"][0]["display_name"] == "Gi0/0"


def test_legacy_shape_and_csv(fleet):
    client = make_client(ADMIN_VIS, _fleet_db())
    legacy = client.get(URL + "/capacity", params={"hours": 24, "limit": 3}).json()
    assert isinstance(legacy, list) and len(legacy) == 3
    first = legacy[0]
    assert first["ifindex"] == 1 and first["if_speed"] == 10**7 and first["utilization_p95_pct"] == 96.0
    assert first["p95_bps"] == 9.6e6 and first["direction"] == "in"
    r = client.get(URL + "/capacity", params={"hours": 24, "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("exporter,ifindex,interface") and len(lines) == 62
    assert lines[1].startswith("10.9.0.1,1,Gi0/0")


def test_billing_report_orders_by_billable_and_explains_indicative_lines(fleet):
    client = make_client(ADMIN_VIS, _fleet_db())
    day = {"from": "2026-09-29T00:00:00Z", "to": "2026-09-30T00:00:00Z", "tz": "UTC", "envelope": 1}
    data = client.get(URL + "/capacity/billing", params=day).json()["data"]
    assert data["period"]["complete"] is True and data["period"]["expected_slots"] == 288
    bills = [i["billable_bps"] for i in data["items"]]
    assert None not in bills and bills == sorted(bills, reverse=True), "largest billable figure first"
    wan = next(i for i in data["items"] if i["ifindex"] == 1)
    assert wan["billable_direction"] == "in" and wan["billable_bps"] == 9.6e6 and wan["billing_grade"] is True
    assert wan["billable_util_pct"] == 96.0 and wan["notes"] == []
    assert data["totals"] == {"interfaces": 61, "billing_grade": 61, "indicative": 0}
    merged = client.get(URL + "/capacity/billing", params={**day, "direction_mode": "merged"}).json()["data"]
    assert merged["method"]["overridden"] == ["direction_mode"]
    mw = next(i for i in merged["items"] if i["ifindex"] == 1)
    assert mw["billable_direction"] == "merged" and mw["billable_bps"] == 1.06e7 and mw["billable_util_pct"] is None
    # A whole month with one day of samples: indicative, saying why, with a coverage figure
    # that cannot round onto the threshold, and no percentile stated at all.
    month = client.get(URL + "/capacity/billing", params={"month": "2026-09", "tz": "UTC", "envelope": 1}).json()["data"]
    assert month["period"]["month"] == "2026-09" and month["period"]["expected_slots"] == 30 * 288
    w = next(i for i in month["items"] if i["ifindex"] == 1)
    assert w["billing_grade"] is False and w["billable_bps"] is None
    assert w["notes"] == ["Coverage 3.33% is below the 99% billing minimum", "Coverage too low for the percentile"]
    csv_r = client.get(URL + "/capacity/billing", params={"month": "2026-09", "tz": "UTC", "format": "csv"})
    assert csv_r.status_code == 200 and "no (indicative)" in csv_r.text and csv_r.text.count("\n") == 62


def test_pct_text_never_rounds_onto_a_threshold():
    assert cap._pct_text(98.996) == "98.99%" and cap._pct_text(0.289) == "0.28%" and cap._pct_text(100.0) == "100%"
    assert cap._pct_text(None) == "unknown"


def test_validation_is_400_with_string_detail(fake_env):
    fake_env.setattr(C, "get_ch_client", lambda: FakeCH())
    client = make_client(ADMIN_VIS, FakeDB())
    for params in ({"sort": "bogus"}, {"order": "up"}, {"state": "fine"}, {"resolution": "x"}, {"role": "core"},
                   {"format": "xml"}, {"limit": 0}, {"port": "x"}):
        r = client.get(URL + "/capacity", params={**params, "envelope": 1})
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), params
    r = client.get(URL + "/capacity/trend", params={"exporter": "not-an-ip", "ifindex": 1})
    assert r.status_code == 400
    r = client.get(URL + "/capacity/trend", params={"exporter": "10.1.1.1", "ifindex": 1, "days": 401})
    assert r.status_code == 400
    r = client.get(URL + "/capacity/billing", params={"month": "2026-13"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_window"
    r = client.get(URL + "/capacity/billing", params={"percentile": 99})
    assert r.status_code == 400


def test_traffic_filters_are_disclosed_as_not_applied(fake_env):
    ch = FakeCH()
    fake_env.setattr(C, "get_ch_client", lambda: ch)
    client = make_client(ADMIN_VIS, FakeDB())
    meta = client.get(URL + "/capacity", params={"envelope": 1, "protocol": 6, "exporter": "10.9.0.1",
                                                 "hour": 9}).json()["meta"]
    assert meta["filters"] == {"exporter": "10.9.0.1", "hour": "9"}
    assert "capacity_filters_not_applied" in {w["code"] for w in meta["warnings"]}
    assert meta["window"]["source"] == "interface_rollup" and meta["window"]["granularity_s"] == 300
    stats_sql = [sql for sql, _ in ch.calls if "n_traffic" in sql]
    assert stats_sql and "toHour(slot" in stats_sql[0] and "toHour(timestamp" in stats_sql[0]


SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                        permissions=frozenset({"netflow.view"}))


def test_every_capacity_query_is_scoped(fake_env):
    ch = FakeCH()
    fake_env.setattr(C, "get_ch_client", lambda: ch)
    client = make_client(SCOPED, FakeDB())
    for path, params in (("/capacity", {}), ("/capacity/billing", {}),
                         ("/capacity/trend", {"exporter": "10.9.0.1", "ifindex": 3})):
        ch.calls.clear()
        r = client.get(URL + path, params={**params, "envelope": 1})
        assert r.status_code == 200, (path, r.text)
        flow = [(s, p) for s, p in ch.calls if "system." not in s]
        assert flow, path
        for sql, p in flow:
            assert any(isinstance(v, (list, tuple)) and list(v) == ["10.9.0.1"] for v in p.values()), (path, sql)
            if "flow_records" in sql:
                assert "fw_event NOT IN (1, 3)" in sql
    assert client.get(URL + "/capacity", params={"exporter": "10.9.0.2"}).status_code == 404
    assert client.get(URL + "/capacity/trend", params={"exporter": "10.9.0.2", "ifindex": 1}).status_code == 404


def test_settings_write_needs_manage_and_is_audited(fake_env):
    fake_env.setattr(C, "get_ch_client", lambda: FakeCH())
    db = FakeDB()
    viewer = make_client(VIEW_VIS, db, user=VIEWER, manage=False)
    got = viewer.get(URL + "/capacity/settings").json()
    assert got["settings"]["percentile"] == 95 and got["permissions"]["manage"] is False
    assert viewer.put(URL + "/capacity/settings", json={"percentile": 90}).status_code == 403
    manager = make_client(ADMIN_VIS, db, user=MANAGER, manage=True)
    bad = manager.put(URL + "/capacity/settings", json={"percentile": 99})
    assert bad.status_code == 400 and "percentile" in bad.json()["fields"]
    ok = manager.put(URL + "/capacity/settings", json={"percentile": 90, "direction_mode": "merged"})
    assert ok.status_code == 200, ok.text
    upsert = [p for s, p in db.writes if "system_settings" in s]
    audit = [p for s, p in db.writes if "audit_logs" in s]
    assert upsert and '"percentile": 90' in upsert[0]["value"]
    assert audit and audit[0]["action"] == "netflow.capacity_settings.update"
    assert '"old"' in audit[0]["metadata"] and '"merged"' in audit[0]["metadata"]


def test_override_crud_is_audited_and_validated(fake_env):
    fake_env.setattr(C, "get_ch_client", lambda: FakeCH())
    db = FakeDB()
    manager = make_client(ADMIN_VIS, db, user=MANAGER, manage=True)
    r = manager.put(URL + "/capacity/overrides/10.9.0.1/16", json={"speed_in_bps": 0})
    assert r.status_code == 400
    r = manager.put(URL + "/capacity/overrides/10.9.0.1/16",
                    json={"display_name": " WAN ", "speed_in_bps": 50_000_000, "speed_out_bps": 20_000_000,
                          "role": "wan", "note": "ISP-A"})
    assert r.status_code == 200, r.text
    ins = [p for s, p in db.writes if "netflow_interface_overrides" in s]
    assert ins[0]["ip"] == "10.9.0.1" and ins[0]["display_name"] == "WAN" and ins[0]["speed_out_bps"] == 20_000_000
    audit = [p for s, p in db.writes if "audit_logs" in s]
    assert audit[0]["action"] == "netflow.interface_override.create"
    assert audit[0]["resource_id"] == "10.9.0.1|16" and "speed_in_bps" in audit[0]["metadata"]
    assert manager.delete(URL + "/capacity/overrides/10.9.0.1/16").status_code == 404   # nothing stored
    viewer = make_client(VIEW_VIS, db, user=VIEWER, manage=False)
    assert viewer.put(URL + "/capacity/overrides/10.9.0.1/16", json={"role": "wan"}).status_code == 403
    scoped = make_client(SCOPED, db, user=MANAGER, manage=True)
    assert scoped.put(URL + "/capacity/overrides/10.9.0.2/16", json={"role": "wan"}).status_code == 404


# ── Scratch ClickHouse: the capacity fixtures through the real SQL ────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

REC_COLS = ["timestamp", "received_at", "exporter_ip", "input_if", "output_if", "bytes", "packets",
            "first_switched_ms", "last_switched_ms", "flow_direction", "fields_present", "sampling_interval",
            "protocol"]


def rec(t, exporter, i=1, o=0, nbytes=0.0, dur_s=0, direction=255, sampling=1):
    end_ms = int(t.timestamp() * 1000)
    first = end_ms - dur_s * 1000 if dur_s else 0
    return [t, t, exporter, i, o, int(nbytes), max(1, int(nbytes // 1000)), first, end_ms if dur_s else 0,
            direction, 1 << 31, sampling, 6]


def keepalive(t, exporter):
    """A record on no interface: the exporter was live in this slot."""
    return rec(t, exporter, 0, 0, 100)


def rate_bytes(bps):
    return bps * 300 / 8


@pytest.fixture(scope="module")
def chdb():
    client = scratch_client()
    with flow_db(client) as db:
        for extra in ("flow_if_counters",):
            ddl = client.query(f"SHOW CREATE TABLE zenplus.{extra}").result_rows[0][0]
            client.command(ddl.replace("zenplus.", f"{db}."))
        client.command(f"""CREATE TABLE {db}.snmp_if_metrics_5m (
            device_id UUID, if_index UInt32, timestamp DateTime64(3, 'UTC'), avg_in_bps Float64,
            avg_out_bps Float64, max_in_bps Float64, max_out_bps Float64, sum_in_errors UInt64,
            sum_out_errors UInt64, sum_in_discards UInt64, sum_out_discards UInt64, sample_count UInt32)
            ENGINE = MergeTree ORDER BY (device_id, if_index, timestamp)""")
        yield client, db


@pytest.fixture
def ch(chdb, monkeypatch):
    client, db = chdb
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    monkeypatch.setattr(C, "CH_DB", db)
    C._retention_cache.update(value=None, at=0.0)

    async def no_owners(db_, ips=None):
        return {}

    monkeypatch.setattr(cap, "resolve_exporter_owners", no_owners)
    yield client, db
    C._retention_cache.update(value=None, at=0.0)


BASE = floor_to(datetime.now(UTC) - timedelta(days=2), 86400) + timedelta(hours=11)


def _insert(client, db, rows):
    for i in range(0, len(rows), 20000):
        client.insert(f"{db}.flow_records", rows[i:i + 20000], column_names=REC_COLS)


def _run(window, exporter, db=None, settings=SETTINGS, business_hours=False, vis=ADMIN_VIS, previous=False,
         f=None):
    async def go():
        return await cap.compute_capacity(window, f or FlowFilter(exporter=exporter), vis, db or FakeDB(),
                                          settings=settings, business_hours=business_hours, previous=previous)

    return asyncio.run(go())


def _row(result, ifindex):
    return next(r for r in result.rows if r["ifindex"] == ifindex)


def _win(start, end):
    return Window(start=start, end=end, mode="fixed", tz="UTC")


def test_f1_partial_edges_are_excluded(ch):
    client, db = ch
    e = "10.77.0.1"
    rows = [rec(BASE + timedelta(minutes=5 * k, seconds=150), e, 1, 2, rate_bytes(40e6)) for k in range(-2, 16)]
    _insert(client, db, rows)
    res = _run(_win(BASE + timedelta(minutes=8, seconds=51), BASE + timedelta(minutes=68, seconds=51)), e)
    assert res.sw.n_slots == 11 and res.sw.head_excluded_s == 69 and res.sw.tail_excluded_s == 231
    r1 = _row(res, 1)
    assert r1["samples"] == 11 and r1["coverage_pct"] == 100.0
    assert r1["in"]["avg_bps"] == 40e6 and r1["in"]["max_bps"] == 40e6        # not 36.9 Mbps (x 3600/3900)
    assert r1["in"]["p_bps"] is None and r1["in"]["p_reason"] == "too_few_samples"
    assert r1["out"]["max_bps"] == 0.0 and _row(res, 2)["out"]["avg_bps"] == 40e6


def test_f2_duplex_and_f3_hairpin(ch):
    client, db = ch
    rows = []
    for k in range(24):
        t = BASE + timedelta(hours=3, minutes=5 * k, seconds=100)
        rows.append(rec(t, "10.77.0.2", 7, 8, rate_bytes(600e6)))
        rows.append(rec(t, "10.77.0.2", 8, 7, rate_bytes(600e6)))
        rows.append(rec(t, "10.77.0.3", 5, 5, rate_bytes(10e6)))
    _insert(client, db, rows)
    w = _win(BASE + timedelta(hours=3), BASE + timedelta(hours=5))
    res = _run(w, "10.77.0.2")
    r7 = _row(res, 7)
    assert r7["samples"] == 24 and r7["in"]["p_bps"] == 600e6 and r7["out"]["p_bps"] == 600e6
    assert r7["combined"]["p_bps"] == 1.2e9
    hair = _row(_run(w, "10.77.0.3"), 5)
    assert hair["in"]["p_bps"] == 10e6 and hair["out"]["p_bps"] == 10e6


def test_f4_idle_slots_are_zero_when_the_exporter_is_live(ch):
    client, db = ch
    e = "10.77.0.4"
    start = BASE - timedelta(days=7, hours=11)
    rows = [keepalive(start + timedelta(minutes=5 * k, seconds=30), e) for k in range(2016)]
    busy = random.Random(4).sample(range(2016), 10)
    rows += [rec(start + timedelta(minutes=5 * k, seconds=200), e, 9, 0, rate_bytes(100e6)) for k in busy]
    _insert(client, db, rows)
    r = _row(_run(_win(start, start + timedelta(days=7)), e), 9)
    assert r["samples"] == 2016 and r["gap_slots"] == 0
    assert round(r["in"]["avg_bps"]) == 496032 and r["in"]["p_bps"] == 0.0 and r["in"]["max_bps"] == 100e6


def test_f5_nearest_rank_small_samples(ch):
    client, db = ch
    rows = []
    for k in range(12):
        rows.append(rec(BASE + timedelta(hours=6, minutes=5 * k, seconds=60), "10.77.0.5", 1, 0,
                        rate_bytes((k + 1) * 1e6)))
    for k in range(20):
        rows.append(rec(BASE + timedelta(hours=8, minutes=5 * k, seconds=60), "10.77.0.15", 1, 0,
                        rate_bytes((k + 1) * 1e6)))
    _insert(client, db, rows)
    twelve = _row(_run(_win(BASE + timedelta(hours=6), BASE + timedelta(hours=7)), "10.77.0.5"), 1)
    assert twelve["samples"] == 12 and twelve["in"]["p_bps"] is None and twelve["in"]["max_bps"] == 12e6
    twenty = _row(_run(_win(BASE + timedelta(hours=8), BASE + timedelta(hours=9, minutes=40)), "10.77.0.15"), 1)
    assert twenty["samples"] == 20 and twenty["in"]["p_bps"] == 19e6            # not interpolated 19.05


def test_f6_nearest_rank_is_deterministic(ch):
    client, db = ch
    e = "10.77.0.6"
    start = BASE - timedelta(days=14)
    order = list(range(1, 4001))
    random.Random(6).shuffle(order)
    _insert(client, db, [rec(start + timedelta(minutes=5 * k, seconds=10), e, 1, 0, rate_bytes(v * 1000))
                         for k, v in enumerate(order)])
    sw = cap.complete_slots(start, start + timedelta(minutes=5 * 4000))
    sql, params = cap.stats_sql(sw, FlowFilter(exporter=e), ADMIN_VIS, cap.SlotSelector(), cap.DirectionPolicy(),
                                95, None)
    values = set()
    for threads in (1, 2, 4, 8, 16):
        rows = client.query(cap._select_scalars(sql), parameters=params, settings={"max_threads": threads}).result_rows
        d = dict(zip(cap.SCALAR_COLUMNS, rows[0]))
        values.add((d["samples"], d["in_p"]))
    assert values == {(4000, 3_800_000.0)}


def test_f7_long_flows_are_spread_over_their_duration(ch):
    client, db = ch
    e = "10.77.0.7"
    start = BASE + timedelta(hours=12)
    rows = [keepalive(start + timedelta(minutes=5 * k, seconds=20), e) for k in range(12)]
    end = start + timedelta(minutes=40)
    rows.append(rec(end, e, 3, 0, 8e6 / 8 * 1800, dur_s=1800))
    _insert(client, db, rows)
    r = _row(_run(_win(start, start + timedelta(hours=1)), e), 3)
    assert abs(r["in"]["max_bps"] - 8e6) < 1 and r["in"]["max_bps"] < 48e6   # not 1.8 GB / 300 s
    assert abs(r["in"]["avg_bps"] - 4e6) < 1


def test_f8_corrupt_volume_is_excluded(ch):
    client, db = ch
    e = "10.77.0.8"
    start = BASE + timedelta(hours=14)
    rows = [rec(start + timedelta(minutes=5 * k, seconds=40), e, 1, 0, rate_bytes(1e6)) for k in range(12)]
    rows.append(rec(start + timedelta(minutes=20, seconds=41), e, 1, 0, 5e15))
    _insert(client, db, rows)
    r = _row(_run(_win(start, start + timedelta(hours=1)), e), 1)
    assert r["in"]["max_bps"] == 1e6


def test_collection_gaps_are_excluded_not_zero(ch):
    client, db = ch
    e = "10.77.0.9"
    start = BASE - timedelta(days=1)
    rows = []
    for k in range(288):
        if 100 <= k < 124:          # the exporter is silent for two hours
            continue
        rows.append(rec(start + timedelta(minutes=5 * k, seconds=90), e, 1, 0, rate_bytes(10e6)))
    _insert(client, db, rows)
    r = _row(_run(_win(start, start + timedelta(days=1)), e), 1)
    assert r["samples"] == 264 and r["gap_slots"] == 24 and r["coverage_pct"] == pytest.approx(91.667, abs=0.001)
    assert r["in"]["avg_bps"] == 10e6 and r["in"]["p_bps"] == 10e6             # the gap is not idle traffic


def test_adjacent_half_open_windows_add_up(ch):
    client, db = ch
    e = "10.77.0.10"
    start = BASE + timedelta(hours=16)
    rng = random.Random(10)
    _insert(client, db, [rec(start + timedelta(seconds=rng.randint(0, 7199)), e, 1, 2, rng.randint(1000, 10**7),
                             dur_s=rng.choice([0, 30, 240, 900])) for _ in range(500)])
    mid = start + timedelta(hours=1)
    a = _row(_run(_win(start, mid), e), 1)["in"]["bytes"]
    b = _row(_run(_win(mid, start + timedelta(hours=2)), e), 1)["in"]["bytes"]
    whole = _row(_run(_win(start, start + timedelta(hours=2)), e), 1)["in"]["bytes"]
    assert a + b == pytest.approx(whole, rel=1e-9)


def test_duplicate_direction_export_is_counted_once(ch):
    client, db = ch
    start = BASE + timedelta(hours=18)
    rows = []
    for k in range(24):
        t = start + timedelta(minutes=5 * k, seconds=70)
        # 10.77.0.11 exports the same packets ingress- and egress-monitored.
        rows.append(rec(t, "10.77.0.11", 1, 2, rate_bytes(20e6), direction=0))
        rows.append(rec(t, "10.77.0.11", 1, 2, rate_bytes(20e6), direction=1))
        # 10.77.0.12 exports egress-monitored records only: still counted.
        rows.append(rec(t, "10.77.0.12", 1, 2, rate_bytes(20e6), direction=1))
        # 10.77.0.13 is configured to count both.
        rows.append(rec(t, "10.77.0.13", 1, 2, rate_bytes(20e6), direction=0))
        rows.append(rec(t, "10.77.0.13", 1, 2, rate_bytes(20e6), direction=1))
    _insert(client, db, rows)
    w = _win(start, start + timedelta(hours=2))
    res = _run(w, "10.77.0.11")
    assert _row(res, 1)["in"]["p_bps"] == 20e6
    assert res.exporters[0]["accounting_basis"] == "ingress_records"
    assert _row(_run(w, "10.77.0.12"), 1)["in"]["p_bps"] == 20e6
    both_db = FakeDB({"netflow_exporter_settings": [("10.77.0.13", "both")]})
    assert _row(_run(w, "10.77.0.13", db=both_db), 1)["in"]["p_bps"] == 40e6


def test_slot_selector_and_previous_period(ch):
    client, db = ch
    e = "10.77.0.16"
    start = floor_to(datetime.now(UTC) - timedelta(days=5), 86400)
    rows = []
    for k in range(2 * 288):
        t = start + timedelta(minutes=5 * k, seconds=45)
        rows.append(rec(t, e, 1, 0, rate_bytes(50e6 if t.hour == 9 else 5e6) * (2 if k >= 288 else 1)))
    _insert(client, db, rows)
    day2 = _win(start + timedelta(days=1), start + timedelta(days=2))
    at9 = _run(day2, e, f=FlowFilter(exporter=e, hour=9))
    r = _row(at9, 1)
    assert at9.expected == 12 and r["samples"] == 12 and r["in"]["max_bps"] == 100e6
    prev = _row(_run(day2, e, previous=True), 1)["previous"]
    assert prev["value_bps"] is not None and prev["change_pct"] == pytest.approx(100.0)


def test_raw_fallback_while_the_interface_rollup_lags(monkeypatch):
    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    async def no_owners(db_, ips=None):
        return {}

    with flow_db(client) as db:
        ddl = client.query("SHOW CREATE TABLE zenplus.flow_if_counters").result_rows[0][0]
        client.command(ddl.replace("zenplus.", f"{db}."))
        e = "10.77.0.14"
        now = datetime.now(UTC)
        start = floor_to(now - timedelta(hours=2), 300)
        early = [rec(start + timedelta(minutes=5 * k, seconds=100), e, 1, 0, rate_bytes(10e6)) for k in range(12)]
        client.insert(f"{db}.flow_records", early, column_names=REC_COLS)
        client.command(f"DROP VIEW {db}.flow_interface_5m_mv")          # the rollup stops here
        late = [rec(start + timedelta(minutes=5 * k, seconds=100), e, 1, 0, rate_bytes(10e6)) for k in range(12, 23)]
        client.insert(f"{db}.flow_records", late, column_names=REC_COLS)
        monkeypatch.setattr(C, "get_ch_client", thread_client)
        monkeypatch.setattr(C, "CH_DB", db)
        monkeypatch.setattr(cap, "resolve_exporter_owners", no_owners)
        C._retention_cache.update(value=None, at=0.0)
        try:
            res = _run(_win(start, start + timedelta(minutes=115)), e)
        finally:
            C._retention_cache.update(value=None, at=0.0)
    assert res.raw_from == start + timedelta(minutes=60)
    r = _row(res, 1)
    assert r["samples"] == 23 and r["in"]["p_bps"] == 10e6 and r["in"]["max_bps"] == 10e6


def test_snmp_and_counter_cross_checks(ch):
    client, db = ch
    e = "10.77.0.17"
    dev = "22222222-2222-2222-2222-222222222222"
    start = BASE + timedelta(hours=20)
    rows = [rec(start + timedelta(minutes=5 * k, seconds=100), e, 4, 0, rate_bytes(30e6)) for k in range(24)]
    _insert(client, db, rows)
    client.insert(f"{db}.snmp_if_metrics_5m",
                  [[dev, 4, start + timedelta(minutes=5 * k), 32e6, 1e6, 33e6, 1e6, 0, 0, 0, 0, 10] for k in range(24)],
                  column_names=["device_id", "if_index", "timestamp", "avg_in_bps", "avg_out_bps", "max_in_bps",
                                "max_out_bps", "sum_in_errors", "sum_out_errors", "sum_in_discards",
                                "sum_out_discards", "sample_count"])
    ctr = []
    octets = 10**12
    for k in range(0, 2 * 3600 + 1, 30):                   # every 30 s at 20 Mbps
        ctr.append([start + timedelta(seconds=k), "c1", e, 4, 10**9, 0, 1, octets + int(20e6 / 8 * k), 5 * k])
    client.insert(f"{db}.flow_if_counters", ctr, column_names=["ts", "collector_id", "exporter_ip", "ifindex",
                                                               "if_speed", "if_direction", "oper_up", "in_octets",
                                                               "out_octets"])

    async def owners(db_, ips=None):
        return {e: ExporterOwner(exporter=e, device_id=dev, hostname="r-17", source="ip-match")}

    cap.resolve_exporter_owners, orig = owners, cap.resolve_exporter_owners
    try:
        inv = FakeDB({"FROM device_interfaces": [(dev, 4, "Gi0/4", None, None, None, None)]})
        res = _run(_win(start, start + timedelta(hours=2)), e, db=inv)
        row = _row(res, 4)
        assert row["speed"]["in_bps"] == 10**9 and row["speed"]["source_in"] == cap.SOURCE_SFLOW
        asyncio.run(cap.snmp_crosscheck([row], res.sw, res.selector, 95))
        asyncio.run(cap.counter_crosscheck([row], res.sw, ADMIN_VIS, 95))
    finally:
        cap.resolve_exporter_owners = orig
    assert row["snmp"]["status"] == "ok" and row["snmp"]["in_p_bps"] == 32e6
    assert row["snmp"]["in_delta_pct"] == pytest.approx(-6.2, abs=0.1)
    c = row["counters"]
    assert c["status"] == "ok" and c["in_p_bps"] == pytest.approx(20e6, rel=1e-6)
    assert c["out_p_bps"] == pytest.approx(40.0, rel=1e-6) and c["in_utilization_pct"] == pytest.approx(2.0)


def test_trend_and_forecast_over_real_history(ch):
    client, db = ch
    e = "10.77.0.18"
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=21)
    rows = []
    for d in range(21):
        rate = 100e6 + 10e6 * d
        for k in range(288):
            rows.append(rec(start + timedelta(days=d, minutes=5 * k, seconds=50), e, 2, 0, rate_bytes(rate)))
    _insert(client, db, rows)

    async def go():
        inv = FakeDB({"netflow_interface_overrides": [(e, 2, "WAN-18", 10**9, 10**9, "wan", None, None, None)]})
        w = Window(start=start - timedelta(hours=6), end=datetime.now(UTC).replace(microsecond=0),
                   mode="relative", tz="UTC")
        return await cap.interface_trend(w, e, 2, ADMIN_VIS, inv, settings=SETTINGS)

    t = asyncio.run(go())
    days = {d["day"]: d for d in t["days"]}
    first = days[start.date().isoformat()]
    assert first["complete"] and first["samples"] == 288 and first["in"]["p_bps"] == 100e6
    assert days[(start - timedelta(days=1)).date().isoformat()]["complete"] is False
    fc = t["forecast"]["in"]
    assert fc["status"] == "ok" and fc["days_used"] == 21 and fc["slope_bps_per_day"] == pytest.approx(10e6)
    # A perfect fit over three weeks: medium (high needs four weeks of history).
    assert fc["confidence"] == "medium" and fc["r2"] == 1.0 and fc["days_to_warn"] == pytest.approx(39.0, abs=0.01)
    assert t["forecast"]["out"]["status"] in ("not_growing", "ok")
    assert t["display_name"] == "WAN-18" and t["speed"]["source_in"] == "override"


# ── Live API (own uvicorn of this tree) ───────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
live = pytest.mark.skipif(not LIVE, reason="set NETFLOW_TEST_API to a running API of this tree")


@pytest.fixture(scope="module")
def live_auth():
    token = os.environ.get("NETFLOW_TEST_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{LIVE}/api/v1/auth/login", json={
        "username": os.environ.get("NETFLOW_TEST_USER", "admin"),
        "password": os.environ.get("NETFLOW_TEST_PASS", "admin123"),
    }, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _live(path, auth, **params):
    return requests.get(f"{LIVE}/api/v1/netflow{path}", params=params, headers=auth, timeout=120)


@live
def test_live_capacity_v2_summary_covers_every_interface(live_auth):
    full = _live("/capacity", live_auth, hours=24, envelope=1, limit=500).json()
    one = _live("/capacity", live_auth, hours=24, envelope=1, limit=1).json()
    assert full["meta"]["window"]["source"] in ("interface_rollup", "hybrid")
    assert one["data"]["summary"] == full["data"]["summary"]
    total = full["data"]["summary"]["interfaces_total"]
    assert total == len([r for r in full["data"]["items"]]) and total >= 1 and len(one["data"]["items"]) == 1
    assert sum(full["data"]["summary"]["by_resolution"].values()) == total
    for row in full["data"]["items"]:
        assert row["coverage_pct"] is None or 0 <= row["coverage_pct"] <= 100
        if row["speed"]["in_bps"] is None:
            assert row["in"]["utilization_p_pct"] is None
    legacy = _live("/capacity", live_auth, hours=24, limit=3).json()
    assert isinstance(legacy, list) and len(legacy) <= 3
    csv_r = _live("/capacity", live_auth, hours=24, format="csv")
    assert csv_r.headers["content-type"].startswith("text/csv") and csv_r.text.count("\n") == total + 1


@live
def test_live_trend_billing_and_settings(live_auth):
    items = _live("/capacity", live_auth, hours=24, envelope=1, limit=1).json()["data"]["items"]
    assert items
    r = items[0]
    t = _live("/capacity/trend", live_auth, exporter=r["exporter"], ifindex=r["ifindex"], days=30, envelope=1)
    assert t.status_code == 200, t.text
    trend = t.json()["data"]
    assert trend["days"] and trend["forecast"]["in"]["status"]
    b = _live("/capacity/billing", live_auth, envelope=1).json()
    assert b["data"]["period"]["month"] and "billing_grade_rule" in b["data"]["method"]
    s = _live("/capacity/settings", live_auth).json()
    assert s["settings"]["percentile"] in cap.PERCENTILES
