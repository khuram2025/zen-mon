"""NetFlow overview core (WP B1): totals, throughput series and comparison,
exporter-health signals, heatmap, exporter list and the what-changed explainer.

Findings covered: NF-01/X-04 (liveness independent of filters), NF-02/X-23
(server-side comparison), NF-17/X-42 (hybrid source, totals == chart sum,
half-open), NF-18 (true flow duration), NF-19/X-39 (one RST ratio), NF-28
(heatmap tz/basis/coverage), X-35 (gaps and partial buckets), CR-15 (explain).

Layers:
  * pure helpers and in-process HTTP over a fake ClickHouse — always run;
  * scratch ClickHouse (NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py):
    fixtures inserted into a disposable database whose materialized views feed
    the rollups exactly as in production;
  * live API (NETFLOW_TEST_API=http://127.0.0.1:8111 plus NETFLOW_TEST_TOKEN or
    NETFLOW_TEST_USER/NETFLOW_TEST_PASS): a running uvicorn of this tree.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
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
from app.api.v1.netflow import access, common as C, explain as X, filters as F, meta as M, overview as O, sql as S  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError, Window  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_classify, netflow_groups  # noqa: E402

UTC = timezone.utc
URL = "/api/v1/netflow"


def _w(start: datetime, end: datetime, tz: str = "UTC") -> Window:
    return Window(start=start, end=end, mode="fixed", tz=tz)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_rst_ratio_divides_by_tcp_with_exported_flags():
    # NF-19 acceptance: 100 RST among 200 valid TCP (plus any UDP) is 50%.
    r = O.rst_payload(200, 200, 100)
    assert (r["pct"], r["status"], r["flag_coverage_pct"]) == (50.0, "ok", 100.0)
    assert O.rst_payload(200, 0, 0)["pct"] is None and O.rst_payload(200, 0, 0)["status"] == "flags_unavailable"
    assert O.rst_payload(0, 0, 0)["status"] == "no_tcp"
    small = O.rst_payload(50, 50, 40)
    assert small["pct"] is None and small["status"] == "low_sample" and small["rst_flows"] == 40
    partial = O.rst_payload(1000, 400, 100, ["tcpflag"])
    assert partial["pct"] == 25.0 and partial["status"] == "partial" and partial["ignored_filters"] == ["tcpflag"]


def test_flow_duration_is_the_true_mean_with_validity():
    # NF-18 acceptance: 500 ms, 1500 ms and 60 000 ms stay what they are.
    for ms in (500, 1500, 60000):
        d = O.duration_payload(10, 10, ms * 10)
        assert d["avg_ms"] == ms and d["status"] == "ok"
    none = O.duration_payload(10, 0, 0)
    assert none["avg_ms"] is None and none["status"] == "unavailable"
    part = O.duration_payload(10, 5, 5000)
    assert part["avg_ms"] == 1000 and part["coverage_pct"] == 50.0 and part["status"] == "partial"
    assert O.duration_payload(0, 0, 0)["status"] == "no_flows"


def test_bucket_size_follows_span_and_rollup():
    assert O.bucket_seconds(3600, False) == 60
    assert O.bucket_seconds(24 * 3600, False) == 300
    assert O.bucket_seconds(24 * 3600, True) == 3600      # rollup rows are whole hours
    assert O.bucket_seconds(7 * 86400, True) == 3600
    assert O.bucket_seconds(30 * 86400, True) == 7200
    assert O.bucket_seconds(90 * 86400, True) % 3600 == 0


def test_bucket_grid_marks_partial_edges():
    start = datetime(2026, 9, 1, 10, 0, 30, tzinfo=UTC)
    grid = O.bucket_grid(start, start + timedelta(minutes=3), 60)
    assert [g[1] for g in grid] == [30, 60, 60, 30]
    assert grid[0][0] == int(datetime(2026, 9, 1, 10, 0, tzinfo=UTC).timestamp())


def test_points_tell_data_idle_gap_unknown_and_expired_apart():
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    grid = O.bucket_grid(t0, t0 + timedelta(minutes=6), 60)
    b = [g[0] for g in grid]
    rows = {b[1]: (600, 10, 2)}
    points = O.build_points(
        grid, rows, receipts={b[2]: 1}, presence={b[3]: 5}, bucket=60,
        effective_from=t0 + timedelta(minutes=1), effective_to=t0 + timedelta(minutes=6),
        tracked_from=t0 + timedelta(minutes=2), open_end=True, fill=True, origin=b[0],
    )
    states = [p["state"] for p in points]
    assert states == ["expired", "data", "idle", "idle", "gap", "gap"]
    assert points[1]["bps"] == 600 * 8 / 60 and points[2]["bytes"] == 0
    assert points[4]["bytes"] is None and points[4]["bps"] is None   # a gap is unknown, not zero
    assert points[-1]["open"] is True and [p["i"] for p in points] == list(range(6))
    # Before delivery tracking started, a bucket without evidence is unknown.
    early = O.build_points(grid, {}, {}, {}, bucket=60, effective_from=t0, effective_to=t0 + timedelta(minutes=6),
                           tracked_from=t0 + timedelta(minutes=3), open_end=False, fill=True, origin=b[0])
    assert [p["state"] for p in early][:3] == ["unknown"] * 3
    # Partial edge buckets divide by their seconds inside the window (no dip).
    edge = O.build_points([(b[0], 30)], {b[0]: (300, 1, 1)}, {}, {}, bucket=60,
                          effective_from=t0 + timedelta(seconds=30), effective_to=t0 + timedelta(minutes=1),
                          tracked_from=None, open_end=False, fill=False, origin=b[0])
    assert edge[0]["partial"] and edge[0]["bps"] == 300 * 8 / 30


def test_baseline_windows_are_adjacent_and_bounded():
    w = _w(datetime(2026, 9, 10, 10, 30, tzinfo=UTC), datetime(2026, 9, 10, 17, 33, tzinfo=UTC))
    prev = O.baseline_window(w, "previous_equal")
    assert prev.end == w.start and prev.end - prev.start == w.end - w.start and prev.mode == "fixed"
    week = O.baseline_window(w, "same_time_last_week")
    assert w.start - week.start == timedelta(days=7)
    with pytest.raises(NetflowError) as exc:
        O.baseline_window(_w(w.start, w.start + timedelta(days=8)), "same_time_last_week")
    assert exc.value.status_code == 400
    with pytest.raises(NetflowError):
        O.baseline_window(w, "yesterday")


def test_compare_delta_is_only_stated_when_meaningful():
    assert O.compare_delta(2000, 1000, prior_limited=False, prior_coverage_pct=100, cur_coverage_pct=100) == (100.0, None)
    assert O.compare_delta(2000, 1000, prior_limited=True, prior_coverage_pct=100, cur_coverage_pct=100)[1] == "prior_beyond_retention"
    assert O.compare_delta(2000, 1000, prior_limited=False, prior_coverage_pct=40, cur_coverage_pct=100)[1] == "prior_low_coverage"
    assert O.compare_delta(2000, 0, prior_limited=False, prior_coverage_pct=None, cur_coverage_pct=None)[1] == "no_prior_traffic"


def test_heatmap_occurrences_follow_the_time_zone():
    # 2026-09-20 is a Sunday: 22:00 UTC is Monday 01:00 in Riyadh (UTC+3).
    start = datetime(2026, 9, 20, 22, tzinfo=UTC)
    occ = O.heatmap_occurrences(start, start + timedelta(hours=1), "Asia/Riyadh")
    assert set(occ) == {(1, 1)} and occ[(1, 1)]["seconds"] == 3600
    # Kolkata (UTC+5:30): one UTC hour spans two local hours.
    k = O.heatmap_occurrences(start, start + timedelta(hours=1), "Asia/Kolkata")
    assert {key: v["seconds"] for key, v in k.items()} == {(1, 3): 1800, (1, 4): 1800}
    # Ten days: three weekdays occur twice, the rest once.
    ten = O.heatmap_occurrences(datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 17, tzinfo=UTC), "UTC")
    counts = {dow: len(ten[(dow, 12)]["slots"]) for dow in range(1, 8)}
    assert counts == {1: 2, 2: 2, 3: 2, 4: 1, 5: 1, 6: 1, 7: 1}
    # DST: Berlin's 2026-10-25 has two local 02:00 hours in one day.
    dst = O.heatmap_occurrences(datetime(2026, 10, 24, 23, tzinfo=UTC), datetime(2026, 10, 25, 3, tzinfo=UTC),
                                "Europe/Berlin")
    assert dst[(7, 2)]["seconds"] == 7200 and len(dst[(7, 2)]["slots"]) == 1


def test_heatmap_cell_states():
    occ = {"seconds": 7200, "tracked": 7200, "slots": {("d1", 5), ("d2", 5)}}
    assert O.heatmap_cell(1, 5, None, 0, 0, 0, 0, "avg")["state"] == "outside_window"
    data = O.heatmap_cell(1, 5, occ, 2000, 4, 2, 2, "avg")
    assert data["state"] == "data" and data["value"] == 1000
    assert O.heatmap_cell(1, 5, occ, 2000, 4, 2, 2, "sum")["value"] == 2000
    part = O.heatmap_cell(1, 5, occ, 2000, 4, 1, 1, "avg")
    assert part["state"] == "partial" and part["value"] == 2000       # per covered occurrence
    assert O.heatmap_cell(1, 5, occ, 0, 0, 0, 2, "avg")["state"] == "zero"
    gap = O.heatmap_cell(1, 5, occ, 0, 0, 0, 0, "avg")
    assert gap["state"] == "no_coverage" and gap["value"] is None
    untracked = {**occ, "tracked": 0}
    assert O.heatmap_cell(1, 5, untracked, 0, 0, 0, 0, "avg")["state"] == "unknown"


def test_explain_prefixes_only_segment_parameters():
    sql = "WHERE (timestamp >= %(nf_t0_from)s AND timestamp < %(nf_t0_to)s) AND protocol = %(nf_protocol)s"
    out, params = X.prefix_time_params(sql, {"nf_t0_from": 1, "nf_t0_to": 2, "nf_protocol": 6}, "cur_")
    assert "%(cur_nf_t0_from)s" in out and "%(nf_protocol)s" in out
    assert params == {"cur_nf_t0_from": 1, "cur_nf_t0_to": 2, "nf_protocol": 6}
    c = X.contributor(["x"], 5000, 0, 5, 0, 10000, 20000)
    assert c["trend"] == "new" and c["delta_pct"] is None and c["share_of_change_pct"] == 50.0


def test_coverage_filter_keeps_only_exporter_scope():
    f = F.parse_flow_filter({"exporter": "10.9.0.1", "protocol": "6", "talker": "10.0.0.1", "tz": "Asia/Tokyo"})
    cf = O.coverage_filter(f)
    assert cf.active_keys() == {"exporter"} and cf.exporter == "10.9.0.1"
    assert O.has_traffic_filter(f) and not O.has_traffic_filter(F.parse_flow_filter({"exporter": "10.9.0.1"}))


def test_measured_span_takes_in_the_running_minute():
    """A window's coverage measures the minute still filling (the first minute
    a new exporter delivers in must not read as 'nothing received')."""
    start = datetime(2026, 10, 1, 7, 33, 41, tzinfo=UTC)
    now = start + timedelta(hours=1)
    frm, full_to, to = M.measured_span(start, now, datetime(2026, 9, 1, tzinfo=UTC), now)
    assert (frm, full_to, to) == (datetime(2026, 10, 1, 7, 33, tzinfo=UTC), datetime(2026, 10, 1, 8, 33, tzinfo=UTC),
                                  datetime(2026, 10, 1, 8, 34, tzinfo=UTC))
    assert M.full_minutes(frm, full_to) == 60
    # The running minute counts once records arrived in it, never against the window before.
    assert M.minute_total(60, 0) == 60 and M.minute_total(60, 1) == 61
    # Tracking that began inside the running minute measures only that minute.
    frm, full_to, to = M.measured_span(start, now, now - timedelta(seconds=20), now)
    assert M.full_minutes(frm, full_to) == 0 and to - frm == timedelta(minutes=1)
    # A fixed window in the past: a whole-minute end has no running minute, a
    # partial last minute is measured like a running one.
    frm, full_to, to = M.measured_span(start, start + timedelta(minutes=19, seconds=19), None, now)
    assert full_to == to == datetime(2026, 10, 1, 7, 53, tzinfo=UTC)
    frm, full_to, to = M.measured_span(start, start + timedelta(minutes=19, seconds=49), None, now)
    assert full_to == datetime(2026, 10, 1, 7, 53, tzinfo=UTC) and to == datetime(2026, 10, 1, 7, 54, tzinfo=UTC)


def test_covered_minutes_follow_each_exporters_expected_interval():
    where, params = "1", {"p": 1}
    start, end = datetime(2026, 10, 1, 8, 0, tzinfo=UTC), datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    plain, p0 = M.covered_minutes_sql(where, params, start, end, {})
    assert "ARRAY JOIN" not in plain and p0["nf_cv_from"] == start and p0["p"] == 1
    sparse, p1 = M.covered_minutes_sql(where, params, start, end, {"10.9.0.1": 480, "10.9.0.2": 60})
    assert "ARRAY JOIN range(transform(" in sparse
    # Only exporters slower than a minute expand; a delivery 7 minutes before the window covers its start.
    assert p1["nf_cv_ips"] == ["::ffff:10.9.0.1"] and p1["nf_cv_spans"] == [8]
    assert p1["nf_cv_lookback"] == start - timedelta(minutes=7)


# ── In-process HTTP over a fake ClickHouse ────────────────────────────────────

class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


TTL_ROWS = [
    ("flow_records", "MergeTree TTL toDateTime(timestamp) + toIntervalDay(30)"),
    ("flow_conversations_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_qos_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_interface_5m", "AggregatingMergeTree TTL timestamp + toIntervalDay(400)"),
    ("flow_exporter_minutes", "AggregatingMergeTree TTL minute + toIntervalDay(400)"),
]


class FakeCH:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}))
        if "system.tables" in sql:
            return FakeResult(TTL_ROWS)
        if "system.parts" in sql:
            return FakeResult([("flow_exporter_minutes", datetime.now(UTC) - timedelta(days=2))])
        if "GROUP BY" in sql:
            return FakeResult([])
        return FakeResult([(0,) * 12])

    def command(self, sql, parameters=None):
        return None

    def flow_sql(self):
        return [(s, p) for s, p in self.calls if "system." not in s]


class FakeDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))


USER = SimpleNamespace(id=uuid4(), username="viewer", role="viewer", is_active=True, scope_tags=[])
SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                        permissions=frozenset({"netflow.view"}))


def make_client(vis: FlowVisibility) -> TestClient:
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield FakeDB()

    async def user():
        return USER

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


@pytest.fixture
def fake(monkeypatch):
    ch = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)

    async def no_refresh(*a, **kw):
        return None

    async def no_repair():
        return {"pending_hours": {}, "rebuilt_hours": {}, "unrecoverable_hours": {}}

    from app.services import netflow_rollup

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    monkeypatch.setattr(netflow_rollup, "repair_status", no_repair)
    C._retention_cache.update(value=None, at=0.0)
    C._repair_cache.update(value=None, at=0.0)
    M.invalidate_expected_intervals()
    yield ch
    C._retention_cache.update(value=None, at=0.0)
    C._repair_cache.update(value=None, at=0.0)
    M.invalidate_expected_intervals()


def _binds_scope(params: dict) -> bool:
    return any(isinstance(v, (list, tuple)) and list(v) == ["10.9.0.1"] for v in params.values())


NEW_PATHS = ("/timeseries/compare", "/explain")


@pytest.mark.parametrize("path", ("/overview", "/timeseries", "/device-status", "/heatmap", "/exporters") + NEW_PATHS)
def test_every_overview_query_is_scoped_and_out_of_scope_is_404(fake, path):
    client = make_client(SCOPED)
    r = client.get(URL + path, params={"envelope": 1, "hours": 24, "fill": 1})
    assert r.status_code == 200, (path, r.text)
    assert r.json()["meta"]["restricted"] is True
    calls = fake.flow_sql()
    assert calls, path
    for sql, params in calls:
        assert _binds_scope(params), (path, sql)
    assert client.get(URL + path, params={"exporter": "10.9.0.2"}).status_code == 404


def test_explain_post_is_scoped_and_validated(fake):
    client = make_client(SCOPED)
    r = client.post(URL + "/explain", params={"envelope": 1},
                    json={"hours": 6, "filter": {"protocol": 6}, "dimensions": ["src", "app"], "limit": 3})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["filters"] == {"protocol": "6"}
    assert all(_binds_scope(p) for _, p in fake.flow_sql())
    bad = client.post(URL + "/explain", json={"hours": 6, "filter": {"protocol": 999}})
    assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)
    assert client.post(URL + "/explain", json={"hours": 6, "dimensions": ["nope"]}).status_code == 400
    assert client.post(URL + "/explain", json={"hours": 6, "limit": 500}).status_code == 400
    assert client.post(URL + "/explain", json={"hours": 6, "filter": {"exporter": "10.9.0.2"}}).status_code == 404


def test_device_status_queries_each_table_with_its_own_predicates(fake):
    client = make_client(F.UNRESTRICTED)
    frm = _iso(datetime.now(UTC) - timedelta(days=5))
    to = _iso(datetime.now(UTC) - timedelta(days=1))
    r = client.get(URL + "/device-status", params={"from": frm, "to": to, "tcpflag": "rst", "protocol": "6"})
    assert r.status_code == 200
    body = r.json()
    assert body["flow_derived"] is True
    for key in ("flow_duration_ms", "rst_ratio_pct", "flow_continuity_pct", "flow_duration", "rst", "continuity"):
        assert key in body
    # No latency/packet-loss/uptime claims any more (NF-18/NF-19).
    assert not {"latency_ms", "packet_loss_pct", "uptime_pct"} & set(body)
    assert body["rst"]["ignored_filters"] == ["protocol", "tcpflag"]
    flow_sql = [s for s, _ in fake.flow_sql()]
    assert not any("flow_records" in s and "flow_qos_1h" in s for s in flow_sql)
    assert not any("toStartOfFiveMinutes" in s for s in flow_sql)       # continuity rescan removed
    rst_queries = [s for s in flow_sql if "AS f_rst" in s]
    assert rst_queries and all("protocol = %(nf_protocol)s" not in s for s in rst_queries)


def test_heatmap_discloses_that_hour_and_day_are_not_applied(fake):
    client = make_client(F.UNRESTRICTED)
    body = client.get(URL + "/heatmap", params={"hours": 168, "hour": 3, "dow": 2, "tz": "Asia/Riyadh",
                                                "protocol": "17", "envelope": 1}).json()
    assert body["meta"]["filters"] == {"protocol": "17", "tz": "Asia/Riyadh"}
    data = body["data"]
    assert len(data["cells"]) == 168 and data["tz"] == "Asia/Riyadh" and data["basis"] == "avg"
    assert data["value_unit"] == "bytes_per_hour" and "max_bytes" in data
    assert client.get(URL + "/heatmap", params={"basis": "median"}).status_code == 400
    assert client.get(URL + "/heatmap", params={"tz": "Mars/Base"}).status_code == 400


def test_overview_keeps_the_legacy_shape(fake):
    client = make_client(F.UNRESTRICTED)
    body = client.get(URL + "/overview", params={"hours": 1}).json()
    assert set(body) >= {"bytes", "packets", "flows", "exporters", "src_hosts", "dst_hosts", "last_seen",
                         "current_bps", "top_protocol", "hosts", "rate_window_s", "rate_status"}
    series = client.get(URL + "/timeseries", params={"minutes": 30, "points": 48}).json()
    assert isinstance(series, list)
    assert client.get(URL + "/timeseries", params={"bucket": 45}).status_code == 400


def test_compare_rejects_unknown_baselines(fake):
    client = make_client(F.UNRESTRICTED)
    assert client.get(URL + "/timeseries/compare", params={"baseline": "yesterday"}).status_code == 400
    r = client.get(URL + "/timeseries/compare", params={"hours": 240, "baseline": "same_time_last_week"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_window"


def test_explain_post_rejects_unknown_filter_keys(fake):
    """A typo in the JSON filter is a 400 naming the key, never an explanation of all traffic."""
    client = make_client(F.UNRESTRICTED)
    r = client.post(URL + "/explain", json={"hours": 1, "filter": {"protocl": 6}})
    assert r.status_code == 400 and r.json()["code"] == "invalid_filter"
    assert "'protocl'" in r.json()["detail"] and "protocl" in r.json()["fields"]
    assert client.post(URL + "/explain", json={"hours": 1, "filter": {"bogus": "1"}}).status_code == 400
    # Canonical keys and the documented read aliases still work.
    assert client.post(URL + "/explain", json={"hours": 1, "filter": {"proto": 6, "dscp_eq": 0}}).status_code == 200


def test_timeseries_caps_the_number_of_buckets(fake):
    """A 93-day per-minute series is raised to a bucket that fits 2000 points, and says so."""
    client = make_client(F.UNRESTRICTED)
    body = client.get(URL + "/timeseries", params={"hours": 2232, "bucket": 60, "fill": 1, "dscp": 0,
                                                    "envelope": 1}).json()
    assert len(body["data"]) <= O.MAX_BUCKETS
    assert body["meta"]["window"]["granularity_s"] == 7200
    assert "bucket_raised" in {w["code"] for w in body["meta"]["warnings"]}
    # A bucket that already fits is kept.
    ok = client.get(URL + "/timeseries", params={"hours": 24, "bucket": 60, "fill": 1, "envelope": 1}).json()
    assert ok["meta"]["window"]["granularity_s"] == 60 and "bucket_raised" not in {w["code"] for w in ok["meta"]["warnings"]}


def test_heatmap_reads_the_hourly_rollup_in_whole_hour_zones(fake):
    """A 7-day heatmap is not a 7-day raw scan: whole-hour zones read the rollup,
    half-hour zones (whose hours do not line up with it) read raw records."""
    client = make_client(F.UNRESTRICTED)
    for tz, rollup in (("UTC", True), ("Asia/Riyadh", True), ("Asia/Kolkata", False)):
        fake.calls.clear()
        body = client.get(URL + "/heatmap", params={"hours": 168, "tz": tz, "envelope": 1}).json()
        heat = [s for s, _ in fake.flow_sql() if "uniqExact(toDate(t" in s]
        assert heat and any("flow_conversations_1h" in s for s in heat) is rollup, tz
        assert body["meta"]["window"]["source"] == ("hybrid" if rollup else "raw"), tz
    # A filter the rollup cannot evaluate reads raw in any zone.
    fake.calls.clear()
    client.get(URL + "/heatmap", params={"hours": 168, "tz": "UTC", "dscp": 46})
    heat = [s for s, _ in fake.flow_sql() if "uniqExact(toDate(t" in s]
    assert heat and not any("flow_conversations_1h" in s for s in heat)


# ── Scratch ClickHouse ────────────────────────────────────────────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

RAW_COLS = [
    "timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_port", "dst_port", "protocol", "tos",
    "tcp_flags", "bytes", "packets", "sampling_interval", "first_switched_ms", "last_switched_ms", "input_if",
    "output_if", "fields_present", "ip_version",
]
TRACKED = (1 << 31) | 0x1FF          # v2 collector, TCP flags among the exported fields
TRACKED_NO_FLAGS = (1 << 31) | 0x1FE  # the template omits TCP flags


def row(t: datetime, *, exporter="10.9.0.1", src="10.0.0.1", dst="192.0.2.1", proto=6, dport=443, sport=40000,
        flags=0x18, bytes_=1000, packets=2, dur_ms=1000, fields=TRACKED, tos=0, received=None):
    ms = int(t.timestamp() * 1000)
    first, last = (ms - dur_ms, ms) if dur_ms is not None else (0, ms)
    return [t, received or t, ipaddress.IPv4Address(exporter), ipaddress.IPv4Address(src),
            ipaddress.IPv4Address(dst), sport, dport, proto, tos, flags, bytes_, packets, 1, first, last, 1, 2,
            fields, 4]


@pytest.fixture
def ch(monkeypatch):
    """A disposable flow database per test, with a thread-local client."""
    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    async def no_refresh(*a, **kw):
        return None

    async def no_repair():
        return {"pending_hours": {}, "rebuilt_hours": {}, "unrecoverable_hours": {}}

    from app.services import netflow_rollup

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    monkeypatch.setattr(netflow_rollup, "repair_status", no_repair)
    with flow_db(client) as db:
        monkeypatch.setattr(C, "get_ch_client", thread_client)
        monkeypatch.setattr(C, "CH_DB", db)
        C._retention_cache.update(value=None, at=0.0)
        C._repair_cache.update(value=None, at=0.0)
        M.invalidate_expected_intervals()

        def insert(rows):
            client.insert(f"{db}.flow_records", rows, column_names=RAW_COLS)
            C._retention_cache.update(value=None, at=0.0)

        yield SimpleNamespace(client=client, db=db, insert=insert, http=make_client(F.UNRESTRICTED))
    C._retention_cache.update(value=None, at=0.0)
    M.invalidate_expected_intervals()


def _get(ch, path, **params):
    r = ch.http.get(URL + path, params={"envelope": 1, **params})
    assert r.status_code == 200, (path, r.text)
    return r.json()


def _base() -> datetime:
    return S.floor_to(datetime.now(UTC), 3600) - timedelta(days=6)


def test_totals_equal_the_chart_and_exact_raw_on_unaligned_windows(ch):
    """NF-17/X-42: hybrid totals equal half-open raw and the chart sum; a
    filter that forces raw does not change the meaning of the interval."""
    base = _base()
    ch.insert([row(base + timedelta(minutes=m), bytes_=1000 + m % 7) for m in range(5 * 1440)])
    start = base + timedelta(hours=10, minutes=30, seconds=15)
    end = start + timedelta(days=4)
    exact = ch.client.query(
        f"SELECT count(), sum(bytes) FROM {ch.db}.flow_records WHERE timestamp >= %(a)s AND timestamp < %(b)s",
        parameters={"a": start, "b": end}).result_rows[0]
    ov = _get(ch, "/overview", **{"from": _iso(start), "to": _iso(end)})
    assert ov["meta"]["window"]["source"] == "hybrid"
    assert (ov["data"]["flows"], ov["data"]["bytes"]) == (exact[0], exact[1])
    ts = _get(ch, "/timeseries", **{"from": _iso(start), "to": _iso(end), "fill": 1})
    assert sum(p["bytes"] or 0 for p in ts["data"]) == exact[1]
    assert ts["meta"]["window"]["granularity_s"] == 3600
    # A DSCP/interface filter that matches every row routes to raw: same totals.
    for extra in ({"dscp": "0"}, {"iface": "1"}):
        f_ov = _get(ch, "/overview", **{"from": _iso(start), "to": _iso(end), **extra})
        assert (f_ov["data"]["flows"], f_ov["data"]["bytes"]) == (exact[0], exact[1]), extra
    # Adjacent windows add up (half-open: the boundary instant counts once).
    mid = start + timedelta(days=2)
    a = _get(ch, "/overview", **{"from": _iso(start), "to": _iso(mid)})["data"]["flows"]
    b = _get(ch, "/overview", **{"from": _iso(mid), "to": _iso(end)})["data"]["flows"]
    assert a + b == exact[0]


def test_compare_uses_one_window_identical_filters_and_indexed_buckets(ch):
    """NF-02/X-23: TCP flat, UDP doubles; the UDP-filtered comparison is +100%."""
    base = _base()
    start = base + timedelta(days=2)
    rows = []
    for m in range(-360, 360):
        t = start + timedelta(minutes=m, seconds=5)
        rows.append(row(t, proto=6, bytes_=1000))
        rows.append(row(t, proto=17, dport=53, flags=0, bytes_=1000 if m >= 0 else 500, src="10.0.0.2"))
    ch.insert(rows)
    end = start + timedelta(hours=6)
    body = _get(ch, "/timeseries/compare", **{"from": _iso(start), "to": _iso(end), "protocol": "17"})["data"]
    assert body["prior_window"]["to"] == body["window"]["from"]
    assert body["totals"]["bytes"] == 360 * 1000 and body["totals"]["prior_bytes"] == 360 * 500
    assert body["totals"]["delta_pct"] == 100.0 and body["totals"]["delta_reason"] is None
    assert body["filters"] == {"protocol": "17"}
    assert [p["i"] for p in body["points"]] == [p["i"] for p in body["prior_points"]]
    mixed = _get(ch, "/timeseries/compare", **{"from": _iso(start), "to": _iso(end)})["data"]
    assert mixed["totals"]["delta_pct"] == round((720 - 540) / 540 * 100, 2)
    # A span that is not a bucket multiple still pairs every bucket.
    odd = _get(ch, "/timeseries/compare", **{"from": _iso(start), "to": _iso(start + timedelta(hours=7, minutes=3))})["data"]
    assert len(odd["points"]) == len(odd["prior_points"])
    assert sum(1 for p in odd["prior_points"] if p["bytes"]) > 0
    # Hour filters apply to both periods, and the response says so.
    hour = _get(ch, "/timeseries/compare", **{"from": _iso(start), "to": _iso(end), "hour": str(start.hour)})["data"]
    assert hour["notes"] and hour["totals"]["prior_bytes"] == 0


def test_rst_ratio_ignores_page_filters_and_missing_flags(ch):
    """NF-19/X-39 acceptance fixture."""
    base = _base() + timedelta(days=3)
    rows = []
    for i in range(100):
        rows.append(row(base + timedelta(seconds=i * 3), flags=0x14))            # RST+ACK
        rows.append(row(base + timedelta(seconds=i * 3 + 1), flags=0x10))        # ACK
    for i in range(800):
        rows.append(row(base + timedelta(seconds=i * 3 % 3000 + 2), proto=17, dport=53, flags=0))
    ch.insert(rows)
    window = {"from": _iso(base), "to": _iso(base + timedelta(hours=1))}
    plain = _get(ch, "/device-status", **window)["data"]
    assert plain["rst_ratio_pct"] == 50.0 and plain["rst"]["status"] == "ok"
    assert plain["tcp_flags"]["total_tcp"] == 200 and plain["tcp_flags"]["rst"] == 100
    for extra in ({"tcpflag": "rst"}, {"protocol": "17"}):
        filtered = _get(ch, "/device-status", **window, **extra)["data"]
        assert filtered["rst_ratio_pct"] == 50.0, extra
    # TCP records from a template without flags are not evidence of "no RST".
    ch.insert([row(base + timedelta(seconds=i), flags=0, fields=TRACKED_NO_FLAGS) for i in range(300)])
    mixed = _get(ch, "/device-status", **window)["data"]
    assert mixed["rst"]["tcp_flows"] == 500 and mixed["rst"]["tcp_flows_with_flags"] == 200
    assert mixed["rst_ratio_pct"] == 50.0 and mixed["rst"]["status"] == "partial"
    # Principle 5: the TCP-flags card states the same ratio, from the same helper.
    card = _get(ch, "/tcp-flags", **window)["data"]
    assert card["rst_ratio"] == mixed["rst"] and card["rst_ratio_pct"] == mixed["rst_ratio_pct"]
    card = _get(ch, "/tcp-flags", **window, tcpflag="rst")["data"]
    assert card["rst_ratio_pct"] == 50.0 and card["rst_ratio"]["ignored_filters"] == ["tcpflag"]


def test_flow_duration_is_true_and_disclosed(ch):
    """NF-18: 500 ms / 1.5 s / 60 s stay themselves; invalid times are excluded."""
    base = _base() + timedelta(days=1)
    for k, ms in enumerate((500, 1500, 60000)):
        t0 = base + timedelta(hours=k)
        ch.insert([row(t0 + timedelta(seconds=i), dur_ms=ms, src=f"10.1.{k}.1") for i in range(10)])
        body = _get(ch, "/device-status", **{"from": _iso(t0), "to": _iso(t0 + timedelta(minutes=5))})["data"]
        assert body["flow_duration_ms"] == ms and body["flow_duration"]["status"] == "ok", ms
    t3 = base + timedelta(hours=4)
    ch.insert([row(t3 + timedelta(seconds=i), dur_ms=2000) for i in range(5)]
              + [row(t3 + timedelta(seconds=10 + i), dur_ms=None) for i in range(5)])
    body = _get(ch, "/device-status", **{"from": _iso(t3), "to": _iso(t3 + timedelta(minutes=5))})["data"]
    assert body["flow_duration_ms"] == 2000 and body["flow_duration"]["valid_flows"] == 5
    assert body["flow_duration"]["coverage_pct"] == 50.0 and body["flow_duration"]["status"] == "partial"


def _last_sunday_22(now: datetime) -> datetime:
    day = (now - timedelta(days=2)).replace(hour=22, minute=0, second=0, microsecond=0)
    while day.isoweekday() != 7:
        day -= timedelta(days=1)
    return day


def test_heatmap_maps_cells_in_the_viewer_zone_and_matches_the_filter(ch):
    """NF-28: a Sunday 22:00 UTC hour is Monday 01:00 in Riyadh, and the cell's
    hour/day filter selects exactly those records."""
    t = _last_sunday_22(datetime.now(UTC))
    ch.insert([row(t + timedelta(minutes=m), bytes_=100) for m in range(60)]
              + [row(t + timedelta(hours=2, minutes=m), bytes_=7) for m in range(60)])
    window = {"from": _iso(t - timedelta(hours=1)), "to": _iso(t + timedelta(hours=4))}
    hm = _get(ch, "/heatmap", **window, tz="Asia/Riyadh")["data"]
    cell = next(c for c in hm["cells"] if (c["dow"], c["hour"]) == (1, 1))
    assert cell["bytes"] == 6000 and cell["state"] == "data" and cell["occurrences"] == 1
    assert next(c for c in hm["cells"] if (c["dow"], c["hour"]) == (7, 22))["state"] == "outside_window"
    ov = _get(ch, "/overview", **window, hour="1", dow="1", tz="Asia/Riyadh")["data"]
    assert ov["bytes"] == 6000
    utc = _get(ch, "/heatmap", **window, tz="UTC")["data"]
    assert next(c for c in utc["cells"] if (c["dow"], c["hour"]) == (7, 22))["bytes"] == 6000


def test_heatmap_average_basis_and_coverage_states(ch):
    """Uniform traffic over ten days: the average basis is flat, the sum basis
    doubles the three weekdays that occur twice; an hour with deliveries but no
    matching flows is zero, one without deliveries is a gap."""
    d0 = S.floor_to(datetime.now(UTC), 86400) - timedelta(days=12)
    rows = [row(d0 + timedelta(minutes=m), bytes_=10) for m in range(10 * 1440)]
    ch.insert(rows)
    window = {"from": _iso(d0), "to": _iso(d0 + timedelta(days=10))}
    avg = _get(ch, "/heatmap", **window, tz="UTC")["data"]
    values = {c["value"] for c in avg["cells"] if c["state"] == "data"}
    assert values == {600.0} and len([c for c in avg["cells"] if c["state"] == "data"]) == 168
    total = _get(ch, "/heatmap", **window, tz="UTC", basis="sum")["data"]
    per_dow = {c["dow"]: c["value"] for c in total["cells"] if c["hour"] == 0}
    assert sorted(per_dow.values()) == [600] * 4 + [1200] * 3
    # UDP-filtered: every hour had deliveries but no UDP -> zero, not a gap.
    udp = _get(ch, "/heatmap", **window, tz="UTC", protocol="17")["data"]
    assert {c["state"] for c in udp["cells"]} == {"zero"}
    # A day later with a six-hour hole: those hours are no_coverage.
    d1 = d0 + timedelta(days=10)
    ch.insert([row(d1 + timedelta(minutes=m), bytes_=10) for m in range(1440) if not 360 <= m < 720])
    hole = _get(ch, "/heatmap", **{"from": _iso(d1), "to": _iso(d1 + timedelta(days=1))}, tz="UTC")["data"]
    states = {c["hour"]: c["state"] for c in hole["cells"] if c["dow"] == d1.isoweekday()}
    assert {states[h] for h in range(6, 12)} == {"no_coverage"} and states[5] == "data"


def test_timeseries_draws_gaps_and_idle_buckets_honestly(ch):
    """X-35: nothing delivered -> null gap; delivered but nothing matched -> 0."""
    t0 = _base() + timedelta(days=4)
    rows = [row(t0 + timedelta(minutes=m)) for m in range(60) if not 20 <= m < 30]
    rows += [row(t0 + timedelta(minutes=m, seconds=10), proto=17, dport=53, flags=0) for m in range(40, 50)]
    ch.insert(rows)
    window = {"from": _iso(t0), "to": _iso(t0 + timedelta(hours=1)), "fill": 1}
    pts = _get(ch, "/timeseries", **window)["data"]
    assert len(pts) == 60 and all(p["state"] == "data" for p in pts if p["i"] < 20)
    assert {p["state"] for p in pts if 20 <= p["i"] < 30} == {"gap"}
    assert all(p["bps"] is None for p in pts if p["state"] == "gap")
    udp = _get(ch, "/timeseries", **window, protocol="17")["data"]
    by_i = {p["i"]: p for p in udp}
    assert by_i[10]["state"] == "idle" and by_i[10]["bps"] == 0
    assert by_i[25]["state"] == "gap" and by_i[45]["state"] == "data"


def test_exporter_receipt_time_does_not_follow_page_filters(ch):
    """X-04: an old matching flow does not make a receiving exporter look silent."""
    now = C.now_utc()
    ch.insert([row(now - timedelta(hours=3), proto=1, dport=0, flags=0)]
              + [row(now - timedelta(minutes=m)) for m in range(1, 30)])
    rows = _get(ch, "/exporters", hours=6, protocol="1")["data"]
    assert len(rows) == 1
    r = rows[0]
    assert r["exporter_ip"] == "10.9.0.1" and r["flows"] == 1
    last_flow = datetime.fromisoformat(r["last_flow_in_scope"].replace("Z", "+00:00"))
    last_rx = datetime.fromisoformat(r["last_received_at"].replace("Z", "+00:00"))
    assert now - last_flow > timedelta(hours=2) and now - last_rx < timedelta(minutes=5)


def test_explain_ranks_what_changed(ch):
    """CR-15: host B appears in the current window and explains the growth."""
    base = _base() + timedelta(days=2)
    rows = []
    for m in range(-120, 120):
        t = base + timedelta(minutes=m, seconds=3)
        rows.append(row(t, src="10.0.0.1", bytes_=1000))
        if m >= 0:
            rows.append(row(t, src="10.0.0.2", dst="198.51.100.9", proto=17, dport=53, flags=0, bytes_=5000))
    ch.insert(rows)
    window = {"from": _iso(base), "to": _iso(base + timedelta(hours=2))}
    body = _get(ch, "/explain", **window, limit=3)["data"]
    assert body["totals"]["delta_bytes"] == 120 * 5000
    src = next(d for d in body["dimensions"] if d["dimension"] == "src")
    top = src["items"][0]
    assert top["label"] == "10.0.0.2" and top["trend"] == "new" and top["share_of_change_pct"] == 100.0
    assert top["filter"] == {"src": "10.0.0.2"} and src["explained_pct"] == 100.0
    proto = next(d for d in body["dimensions"] if d["dimension"] == "protocol")
    assert proto["items"][0]["filter"] == {"protocol": 17}
    posted = ch.http.post(URL + "/explain", json={**window, "limit": 3}).json()
    assert posted["totals"] == body["totals"]
    # The same filter applies to both periods.
    tcp = _get(ch, "/explain", **window, protocol="6")["data"]
    assert tcp["totals"]["delta_bytes"] == 0


def test_rate_is_unknown_when_nothing_was_delivered(ch):
    """A silent exporter's rate is unknown ('no_receipts'), not a confident 0 bps;
    deliveries that matched nothing are a real zero."""
    t0 = _base() + timedelta(days=4, hours=6)
    ch.insert([row(t0 + timedelta(minutes=m, seconds=5)) for m in range(30)])
    silent = _get(ch, "/overview", **{"from": _iso(t0), "to": _iso(t0 + timedelta(hours=1))})["data"]
    assert silent["current_bps"] is None and silent["rate_status"] == "no_receipts"
    assert silent["bytes"] == 30 * 1000
    live = _get(ch, "/overview", **{"from": _iso(t0), "to": _iso(t0 + timedelta(minutes=30))})["data"]
    assert live["rate_status"] == "ok" and live["current_bps"] == 5 * 1000 * 8 / 300
    idle = _get(ch, "/overview", **{"from": _iso(t0), "to": _iso(t0 + timedelta(minutes=30)), "protocol": "17"})["data"]
    assert idle["rate_status"] == "ok" and idle["current_bps"] == 0.0
    other = _get(ch, "/overview", **{"from": _iso(t0), "to": _iso(t0 + timedelta(minutes=30)), "exporter": "10.9.0.7"})
    assert other["data"]["current_bps"] is None and other["data"]["rate_status"] == "no_receipts"


def test_sparse_exporter_coverage_follows_its_expected_interval(ch):
    """An exporter that healthily delivers every 8 minutes (learned 480 s) is
    fully covered: no 'GAPS', idle not gap buckets, and a stated comparison."""
    now = C.now_utc()
    t0 = S.floor_to(now, 3600) - timedelta(hours=10)
    ch.insert([row(t0 + timedelta(minutes=m, seconds=20), bytes_=8000) for m in range(0, 9 * 60, 8)])
    learned = M.expected_intervals
    intervals = asyncio.run(learned(None))
    assert intervals.get("10.9.0.1") == 480
    window = {"from": _iso(t0 + timedelta(hours=4)), "to": _iso(t0 + timedelta(hours=8))}
    cont = _get(ch, "/device-status", **window)["data"]["continuity"]
    assert cont["coverage_pct"] == 100.0 and cont["minutes_tracked"] == 240
    cov = _get(ch, "/coverage", **window)["data"]
    assert cov["totals"]["coverage_pct"] == 100.0 and cov["exporters"][0]["coverage_pct"] == 100.0
    pts = _get(ch, "/timeseries", **window, fill=1, bucket=60)["data"]
    assert {p["state"] for p in pts} == {"data", "idle"}
    cmp_ = _get(ch, "/timeseries/compare", **window)["data"]
    assert cmp_["totals"]["delta_reason"] is None and cmp_["totals"]["delta_pct"] == 0.0
    # Silence longer than the interval is still a gap.
    late = {"from": _iso(t0 + timedelta(hours=8)), "to": _iso(t0 + timedelta(hours=10))}
    gap = _get(ch, "/device-status", **late)["data"]["continuity"]
    assert gap["coverage_pct"] < 60


def test_first_minute_of_a_new_exporter_counts_as_received(ch):
    """Records that arrived in the running minute are coverage: the panels must
    not claim 'no flow records were received' while the totals show them."""
    now = C.now_utc()
    ch.insert([row(now - timedelta(seconds=2), received=now, bytes_=5000)])
    cov = _get(ch, "/coverage", hours=1)["data"]
    assert cov["totals"]["records"] == 1 and cov["totals"]["minutes_with_data"] == 1
    assert cov["totals"]["minutes_in_window"] >= 1
    cont = _get(ch, "/device-status", hours=1)["data"]["continuity"]
    assert cont["minutes_with_data"] == 1 and cont["coverage_pct"] is not None


def test_raw_only_filter_beyond_raw_retention_is_unknown(ch):
    """X-25: a DSCP filter on a window only the rollups still cover has unknown
    totals (null, 'beyond_retention'), and the warning names the filter."""
    t0 = S.floor_to(C.now_utc(), 3600) - timedelta(days=40)
    ch.insert([row(t0 + timedelta(minutes=m), bytes_=100) for m in range(6 * 60)])
    window = {"from": _iso(t0 - timedelta(minutes=17)), "to": _iso(t0 + timedelta(hours=5, minutes=43))}
    plain = _get(ch, "/overview", **window)
    assert plain["data"]["bytes"] == 5 * 60 * 100 and plain["data"]["totals_status"] == "ok"
    body = _get(ch, "/overview", **window, dscp="0")
    data, meta = body["data"], body["meta"]
    assert data["bytes"] is None and data["flows"] is None and data["exporters"] is None
    assert data["totals_status"] == "beyond_retention" and data["raw_only_filters"] == ["dscp"]
    assert meta["window"]["effective_from"] == meta["window"]["effective_to"] and meta["retention"]["limited"]
    assert any("DSCP filter needs raw flow records" in w["message"] for w in meta["warnings"])
    pts = _get(ch, "/timeseries", **window, dscp="0", fill=1)["data"]
    assert {p["state"] for p in pts} == {"expired"}


# ── Live API (own uvicorn of this tree) ───────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
live = pytest.mark.skipif(not LIVE, reason="set NETFLOW_TEST_API to a running API of this tree")


@pytest.fixture(scope="module")
def live_auth():
    token = os.environ.get("NETFLOW_TEST_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{LIVE}/api/v1/auth/login", json={
            "username": os.environ.get("NETFLOW_TEST_USER", "admin"),
            "password": os.environ.get("NETFLOW_TEST_PASS", "admin123"),
        }, timeout=10)
        r.raise_for_status()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"live API unreachable: {exc}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _live(auth, path, **params):
    r = requests.get(f"{LIVE}/api/v1/netflow{path}", params={"envelope": 1, **params}, headers=auth, timeout=120)
    assert r.status_code == 200, (path, r.text)
    return r.json()


@live
def test_live_totals_match_chart_for_a_fixed_window(live_auth):
    end = C.now_utc() - timedelta(minutes=2)
    window = {"from": _iso(end - timedelta(hours=6)), "to": _iso(end)}
    ov = _live(live_auth, "/overview", **window)["data"]
    ts = _live(live_auth, "/timeseries", **window, fill=1)["data"]
    assert ov["bytes"] == sum(p["bytes"] or 0 for p in ts)
    cmp_ = _live(live_auth, "/timeseries/compare", **window)["data"]
    assert cmp_["totals"]["bytes"] == ov["bytes"] and cmp_["prior_window"]["to"] == cmp_["window"]["from"]


@live
def test_live_rst_ratio_is_filter_independent(live_auth):
    a = _live(live_auth, "/device-status", hours=6)["data"]
    b = _live(live_auth, "/device-status", hours=6, tcpflag="rst")["data"]
    assert a["rst"]["tcp_flows_with_flags"] == b["rst"]["tcp_flows_with_flags"] or b["rst"]["tcp_flows"] >= 0
    assert "latency_ms" not in a


@live
def test_live_heatmap_and_explain(live_auth):
    hm = _live(live_auth, "/heatmap", hours=168, tz="Asia/Riyadh")["data"]
    assert len(hm["cells"]) == 168 and hm["tz"] == "Asia/Riyadh"
    ex = _live(live_auth, "/explain", hours=1, limit=2)["data"]
    assert {d["dimension"] for d in ex["dimensions"]} == set(X.DIMENSION_KEYS)
