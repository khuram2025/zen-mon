"""Wave C capacity/breakdowns fixes: regressions for the acceptance failures.

* NF-16  a speed known in one direction only never makes an interface
         "healthy" (state tiles and the coverage card agree);
* FE-05  complete speed history (bulk changes audited per interface, legacy
         batch rows, override values per direction) and per-day speeds;
* NEW    a NetFlow override shadowed by the device circuit speed is reported;
* NEW    the rollup-lag check answers from aggregates and confirms a suspected
         lag from a bounded raw slice only;
* NEW    provisional (still settling) slots are left out, never counted as
         idle zeros, and disclosed;
* X-19   the link-utilization detail names the exporters that carried the
         ifIndex (live tier).

Unit tests always run; ``NETFLOW_TEST_CLICKHOUSE`` enables the scratch
ClickHouse tier and ``NETFLOW_TEST_API`` the live tier (see the capacity
tests for the conventions).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.api.v1.netflow as netflow  # noqa: E402,F401  (import order: router before the service)
from app.api.v1.netflow import common as C  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import Window  # noqa: E402
from app.api.v1.netflow.filters import FlowFilter  # noqa: E402
from app.api.v1.netflow.sql import floor_to  # noqa: E402
from app.services import netflow_capacity as cap  # noqa: E402

UTC = timezone.utc
ADMIN_VIS = FlowVisibility(restricted=False, exporters=None, user_id=None,
                           permissions=frozenset({"system.admin"}))
SETTINGS = cap.DEFAULT_SETTINGS
DEV = "cbcb0000-0000-4000-8000-000000000201"


def _stats(**kw):
    base = dict(exporter="198.18.220.1", ifindex=8, n_traffic=24, n_live=24, samples=24, zeros=0, rnk=23,
                in_p=None, in_max=None, in_sum=0.0, out_p=None, out_max=None, out_sum=0.0,
                sum_p=None, sum_max=None, sum_sum=0.0, in_bytes=0.0, out_bytes=0.0, in_packets=0.0,
                out_packets=0.0, flows=0, last_slot=None)
    base.update(kw)
    return base


def _meta(**kw):
    m = cap.IfaceMeta(exporter=kw.pop("exporter", "198.18.220.1"), ifindex=kw.pop("ifindex", 8))
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def _rate(bps, n=24):
    return dict(in_p=bps, in_max=bps, in_sum=bps * n, out_p=bps, out_max=bps, out_sum=bps * n)


# ── NF-16: one direction's speed unknown ─────────────────────────────────────

def test_partial_speed_is_never_called_healthy():
    # Gi1/8: NetFlow override in = 30 Mbps, out empty; 15 Mbps each way.
    meta = _meta(device_id=DEV, device_interfaces=8, found=True, override={"speed_in_bps": 30_000_000})
    row = cap.build_row(_stats(**_rate(15e6)), meta, 24, SETTINGS)
    h = row["headline"]
    assert h["partial_speed"] is True and h["unknown_directions"] == ["out"]
    assert h["state"] == "unknown", "50 % on the known side does not vouch for the unknown side"
    assert h["utilization_pct"] == 50.0 and h["direction"] == "in"       # the known figure is still reported
    assert row["resolution"] == "speed_unknown" and row["out"]["utilization_p_pct"] is None
    # The state tiles and the coverage card count it the same way: capacity not known.
    summary = cap.summarize([row])
    assert summary["by_state"]["unknown"] == 1 and summary["by_state"]["healthy"] == 0
    assert summary["speed_unknown"] == 1


def test_partial_speed_keeps_a_measured_problem():
    meta = _meta(device_id=DEV, device_interfaces=8, found=True, override={"speed_in_bps": 30_000_000})
    warn = cap.build_row(_stats(**_rate(24e6)), meta, 24, SETTINGS)            # 80 % of the known side
    assert warn["headline"]["state"] == "warning" and warn["headline"]["partial_speed"]
    over = cap.build_row(_stats(**_rate(36e6)), meta, 24, SETTINGS)
    assert over["headline"]["state"] == "over"
    # Both speeds known: healthy as before.
    both = _meta(device_id=DEV, device_interfaces=8, found=True,
                 override={"speed_in_bps": 30_000_000, "speed_out_bps": 30_000_000})
    ok = cap.build_row(_stats(**_rate(15e6)), both, 24, SETTINGS)
    assert ok["headline"]["state"] == "healthy" and ok["headline"]["unknown_directions"] == []
    assert "never called healthy (speed unknown)" in cap.method_block(SETTINGS, cap.SlotSelector(),
                                                                      business_hours=False)["state_rule"]


# ── NEW: an override shadowed by the circuit speed ───────────────────────────

def test_override_shadowed_by_configured_speed_is_reported():
    # Gi1/6: configured 50 Mbps, NetFlow override 200/200 Mbps -> the override is not in use.
    meta = _meta(device_id=DEV, device_interfaces=8, found=True, configured_speed=50_000_000,
                 override={"speed_in_bps": 200_000_000, "speed_out_bps": 200_000_000})
    sb = cap.speed_block(meta)
    assert sb["in_bps"] == 50_000_000 and sb["source_in"] == cap.SOURCE_CONFIGURED
    assert sb["override_in_bps"] == 200_000_000 and sb["override_shadowed"] == ["in", "out"]
    row = cap.build_row(_stats(**_rate(10e6)), meta, 24, SETTINGS)
    assert row["speed"]["override_shadowed"] == ["in", "out"]
    # No circuit speed: the override is in use, nothing shadowed.
    free = _meta(device_id=DEV, device_interfaces=8, found=True,
                 override={"speed_in_bps": 50_000_000, "speed_out_bps": 20_000_000})
    assert cap.speed_block(free)["override_shadowed"] == [] and cap.speed_block(free)["source_out"] == "override"


# ── FE-05: speed history ─────────────────────────────────────────────────────

T0 = datetime(2026, 9, 20, 10, tzinfo=UTC)


def test_parse_speed_change_variants():
    single = cap.parse_speed_change(T0, "device_interface.speed.update", "device_interface",
                                    {"configured_speed_bps": {"old": None, "new": 20_000_000}}, "admin", 4)
    assert single["source"] == "configured" and single["in"] == {"old": None, "new": 20_000_000}
    assert single["old"] is None and single["new"] == 20_000_000 and single["at"] == "2026-09-20T10:00:00Z"
    bulk = cap.parse_speed_change(T0, "device_interface.speed.bulk_update", "device_interface",
                                  {"if_index": 4, "configured_speed_bps": {"old": 20_000_000, "new": 30_000_000}},
                                  "admin", 4)
    assert bulk["in"] == bulk["out"] == {"old": 20_000_000, "new": 30_000_000}
    # A batch row of an older release: {"old": {"<ifIndex>": speed}, "new": speed}.
    legacy = cap.parse_speed_change(T0, "device_interface.speed.bulk_update", "device_interface",
                                    {"if_indexes": [4, 5], "configured_speed_bps": {"old": {"4": 20_000_000,
                                                                                          "5": None},
                                                                                  "new": 30_000_000}},
                                    "admin", 4)
    assert legacy["old"] == 20_000_000 and legacy["new"] == 30_000_000
    # Override entries carry their values per direction (the dialog prints them).
    ov = cap.parse_speed_change(T0, "netflow.interface_override.create", "netflow_interface_override",
                                {"changes": {"speed_in_bps": {"old": None, "new": 50_000_000},
                                             "speed_out_bps": {"old": None, "new": 20_000_000},
                                             "display_name": {"old": None, "new": "DSL"}}}, "admin", 4)
    assert ov["in"] == {"old": None, "new": 50_000_000} and ov["out"] == {"old": None, "new": 20_000_000}
    assert ov["old"] is None and ov["new"] is None                       # asymmetric: no single pair
    same = cap.parse_speed_change(T0, "netflow.interface_override.update", "netflow_interface_override",
                                  {"changes": {"speed_in_bps": {"old": 1, "new": 2},
                                               "speed_out_bps": {"old": 1, "new": 2}}}, None, 4)
    assert same["old"] == 1 and same["new"] == 2
    # A name-only override edit is not a speed change.
    assert cap.parse_speed_change(T0, "netflow.interface_override.update", "netflow_interface_override",
                                  {"changes": {"display_name": {"old": "a", "new": "b"}}}, None, 4) is None


class AuditDB:
    """Answers the audit_logs query; records the statement and parameters."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        rows = self.rows if "audit_logs" in str(statement) else []
        return SimpleNamespace(all=lambda: list(rows))

    def begin_nested(self):
        db = self

        class _N:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _N()


def test_speed_history_includes_bulk_and_override_values():
    rows = [
        (T0 + timedelta(days=2), "device_interface.speed.bulk_update", "device_interface",
         {"if_index": 4, "configured_speed_bps": {"old": 20_000_000, "new": 30_000_000}}, "admin"),
        (T0 + timedelta(days=1), "netflow.interface_override.update", "netflow_interface_override",
         {"changes": {"role": {"old": None, "new": "wan"}}}, "admin"),
        (T0, "device_interface.speed.update", "device_interface",
         {"configured_speed_bps": {"old": None, "new": 20_000_000}}, "admin"),
    ]
    db = AuditDB(rows)
    meta = _meta(exporter="198.18.220.1", ifindex=4, device_id=DEV, configured_speed=30_000_000)
    hist = asyncio.run(cap.speed_history(db, meta))
    assert [(h["source"], h["old"], h["new"]) for h in hist] == [
        ("configured", 20_000_000, 30_000_000), ("configured", None, 20_000_000)]
    sql, params = db.calls[0]
    assert f"{DEV}:4" in params["ids"] and "198.18.220.1|4" in params["ids"]
    # Older batch rows (resource_id '<device>:<n> interfaces') are matched by their metadata.
    assert "if_indexes" in sql and params["dev"] == DEV and params["ifx"] == "[4]"
    assert all(not k.startswith("_") for e in cap.public_history(hist) for k in e)


def test_daily_speed_is_the_speed_in_effect_that_day():
    meta = _meta(exporter="198.18.220.1", ifindex=4, device_id=DEV, device_interfaces=8, found=True,
                 snmp_speed=1_000_000_000, configured_speed=30_000_000)
    hist = [cap.parse_speed_change(T0 + timedelta(days=2), "device_interface.speed.bulk_update", "device_interface",
                                   {"configured_speed_bps": {"old": 20_000_000, "new": 30_000_000}}, None, 4),
            cap.parse_speed_change(T0, "device_interface.speed.update", "device_interface",
                                   {"configured_speed_bps": {"old": None, "new": 20_000_000}}, None, 4)]
    tl = cap.speed_timeline(meta, hist)
    before = cap.speeds_at(meta, tl, T0 - timedelta(hours=1))
    mid = cap.speeds_at(meta, tl, T0 + timedelta(days=1))
    now = cap.speeds_at(meta, tl, T0 + timedelta(days=3))
    assert (before["in_bps"], before["source_in"]) == (1_000_000_000, "snmp")     # before any circuit speed
    assert (mid["in_bps"], mid["out_bps"]) == (20_000_000, 20_000_000)
    assert (now["in_bps"], now["source_in"]) == (30_000_000, "configured")
    # Per-direction override history: 50/20 Mbps set on day 1.
    ov_meta = _meta(exporter="127.0.0.83", ifindex=1, override={"speed_in_bps": 50_000_000,
                                                                "speed_out_bps": 20_000_000})
    ov_hist = [cap.parse_speed_change(T0, "netflow.interface_override.create", "netflow_interface_override",
                                      {"changes": {"speed_in_bps": {"old": None, "new": 50_000_000},
                                                   "speed_out_bps": {"old": None, "new": 20_000_000}}}, None, 1)]
    tl2 = cap.speed_timeline(ov_meta, ov_hist)
    assert cap.speeds_at(ov_meta, tl2, T0 - timedelta(days=1))["in_bps"] is None
    after = cap.speeds_at(ov_meta, tl2, T0 + timedelta(days=1))
    assert (after["in_bps"], after["out_bps"]) == (50_000_000, 20_000_000)


def test_bulk_speed_change_audits_each_interface(monkeypatch):
    from app.api.v1 import devices

    audits: list[dict] = []

    async def fake_audit(db, **kw):
        audits.append(kw)

    class BulkDB:
        async def execute(self, statement, params=None):
            return SimpleNamespace(fetchall=lambda: [(5, None), (4, 20_000_000)])

        async def commit(self):
            return None

    monkeypatch.setattr(devices, "write_audit_log", fake_audit)
    payload = devices.InterfaceBulkSpeedUpdate(if_indexes=[4, 5], configured_speed_bps=30_000_000)
    out = asyncio.run(devices.bulk_update_device_interface_speed(UUID(DEV), payload, BulkDB(),
                                                                 SimpleNamespace(id=uuid4(), username="admin")))
    assert out["updated"] == 2
    assert [a["resource_id"] for a in audits] == [f"{DEV}:4", f"{DEV}:5"]
    assert audits[0]["metadata"]["configured_speed_bps"] == {"old": 20_000_000, "new": 30_000_000}
    assert audits[1]["metadata"]["configured_speed_bps"] == {"old": None, "new": 30_000_000}
    # ...and the capacity history parses them like a single change.
    e = cap.parse_speed_change(T0, audits[0]["action"], audits[0]["resource_type"], audits[0]["metadata"], None, 4)
    assert e["old"] == 20_000_000 and e["new"] == 30_000_000


# ── NEW: rollup lag from aggregates, bounded raw probe ───────────────────────

class LagCH:
    """Fake run_query for rollup_lag: aggregate rows by kind, probe rows."""

    def __init__(self, agg, probe=()):
        self.agg, self.probe = agg, list(probe)
        self.calls: list[tuple[str, dict, str]] = []

    async def __call__(self, sql, params=None, *, label="", **kw):
        self.calls.append((sql, dict(params or {}), label))
        rows = self.probe if label == "capacity-lag-probe" else self.agg
        return SimpleNamespace(result_rows=list(rows))


def _ts(dt):
    return int(dt.timestamp())


def _lag(monkeypatch, fake, now):
    cap._lag_probe_cache.clear()
    monkeypatch.setattr(cap, "run_query", fake)
    sw = cap.complete_slots(now - timedelta(hours=24), now)
    return asyncio.run(cap.rollup_lag(sw, ADMIN_VIS, now=now))


def test_rollup_lag_steady_state_reads_no_raw(monkeypatch):
    now = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)
    fake = LagCH([("r", "198.18.220.1", _ts(datetime(2026, 10, 1, 12, 5, tzinfo=UTC))),
                  ("f", "198.18.220.1", _ts(datetime(2026, 10, 1, 12, 7, 10, tzinfo=UTC)))])
    assert _lag(monkeypatch, fake, now) is None
    assert [c[2] for c in fake.calls] == ["capacity-lag"]
    sql = fake.calls[0][0]
    assert "flow_records" not in sql, "the polling path never scans raw records"
    assert "flow_interface_5m" in sql and "flow_exporter_minutes" in sql


def test_rollup_lag_keepalive_exporter_probes_a_bounded_slice(monkeypatch):
    now = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)
    # 198.18.220.11 delivers only keepalives on no interface: no rollup rows, fresh records.
    fake = LagCH([("f", "198.18.220.11", _ts(now - timedelta(seconds=20)))], probe=[])
    assert _lag(monkeypatch, fake, now) is None
    probe = [c for c in fake.calls if c[2] == "capacity-lag-probe"]
    assert len(probe) == 1
    assert probe[0][1]["cap_probe_from"] >= now - timedelta(seconds=20) - cap.LAG_PROBE_SPAN
    assert "input_if != 0 OR output_if != 0" in probe[0][0]
    # The same question within the cache TTL is not asked again (15-second polling).
    monkeypatch.setattr(cap, "run_query", fake)
    sw = cap.complete_slots(now - timedelta(hours=24), now)
    asyncio.run(cap.rollup_lag(sw, ADMIN_VIS, now=now))
    assert len([c for c in fake.calls if c[2] == "capacity-lag-probe"]) == 1


def test_rollup_lag_detects_a_stalled_rollup(monkeypatch):
    now = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)
    stalled = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    fake = LagCH([("r", "198.18.220.1", _ts(stalled)), ("f", "198.18.220.1", _ts(now - timedelta(seconds=30)))],
                 probe=[("198.18.220.1", _ts(now - timedelta(seconds=30)))])
    assert _lag(monkeypatch, fake, now) == stalled + cap.SLOT
    probe = [c for c in fake.calls if c[2] == "capacity-lag-probe"][0]
    assert probe[1]["cap_probe_from"] == now - timedelta(seconds=30) - cap.LAG_PROBE_SPAN


# ── NEW: provisional slots ───────────────────────────────────────────────────

def test_settle_info_rules():
    assert cap.settle_info(0, 0, None) == cap.SettleInfo(0, "sflow")
    assert cap.settle_info(100, 50, 1_800_000) == cap.SettleInfo(1800, "observed")
    assert cap.settle_info(100, 50, 59_500) == cap.SettleInfo(60, "observed")
    assert cap.settle_info(100, 0, None) == cap.SettleInfo(cap.SETTLE_DEFAULT_S, "default")
    now = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)
    assert cap.settle_at(cap.SettleInfo(1800, "observed"), now) == datetime(2026, 10, 1, 11, 35, tzinfo=UTC)
    assert cap.settle_at(cap.SettleInfo(0, "sflow"), now) == datetime(2026, 10, 1, 12, 5, tzinfo=UTC)


def test_settle_plan_scopes_and_sql(monkeypatch):
    now = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)
    evidence = {"198.18.220.11": cap.SettleInfo(1800, "observed"), "10.0.0.9": cap.SettleInfo(0, "sflow"),
                "10.0.0.7": cap.SettleInfo(1800, "observed")}

    async def fake_evidence():
        return evidence

    monkeypatch.setattr(cap, "exporter_settle_evidence", fake_evidence)
    sw = cap.complete_slots(now - timedelta(hours=1), now)
    scoped = FlowVisibility(restricted=True, exporters=frozenset({"198.18.220.11", "10.0.0.9"}), user_id="u",
                            permissions=frozenset({"netflow.view"}))
    plan = asyncio.run(cap.settle_plan(sw, scoped, now=now))
    assert set(plan.ends) == {"198.18.220.11", "10.0.0.9"}, "another team's exporters are not named"
    assert plan.affected == ["198.18.220.11"]
    assert plan.end_for("198.18.220.11", sw) == datetime(2026, 10, 1, 11, 35, tzinfo=UTC)
    assert plan.end_for("10.0.0.9", sw) == sw.end
    params: dict = {}
    sql = plan.sql("timestamp", "e", params)
    assert "transform(toString(e)" in sql and params["cap_st_k"] == ["::ffff:10.0.0.9", "::ffff:198.18.220.11"]
    assert plan.describe()[0]["basis"] == "observed"
    # The evidence lookup failing never fails the page: nothing is excluded, and it says so.
    async def broken():
        raise RuntimeError("clickhouse busy")

    monkeypatch.setattr(cap, "exporter_settle_evidence", broken)
    assert not asyncio.run(cap.settle_plan(sw, scoped, now=now)).active
    # A window that ended before anything could still change: nothing to exclude, no evidence read.
    old = cap.complete_slots(now - timedelta(hours=5), now - timedelta(hours=3))
    assert not asyncio.run(cap.settle_plan(old, ADMIN_VIS, now=now)).active


# ── Scratch ClickHouse: provisional slots and the lag check through real SQL ──

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

COLS = ["timestamp", "received_at", "exporter_ip", "input_if", "output_if", "bytes", "packets",
        "first_switched_ms", "last_switched_ms", "flow_direction", "fields_present", "sampling_interval",
        "protocol"]


def rec(t, exporter, i=1, o=0, nbytes=1000, first=None, received=None):
    end_ms = int(t.timestamp() * 1000)
    return [t, received or t, exporter, i, o, int(nbytes), max(1, int(nbytes // 1000)),
            int(first.timestamp() * 1000) if first else 0, end_ms if first else 0, 255, 1 << 31, 1, 6]


class EmptyDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None)


@pytest.fixture
def ch(monkeypatch):
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
        monkeypatch.setattr(C, "get_ch_client", thread_client)
        monkeypatch.setattr(C, "CH_DB", db)
        monkeypatch.setattr(cap, "resolve_exporter_owners", no_owners)
        C._retention_cache.update(value=None, at=0.0)
        cap._settle_cache.update(at=None, value=None)
        cap._lag_probe_cache.clear()
        try:
            yield client, db
        finally:
            C._retention_cache.update(value=None, at=0.0)
            cap._settle_cache.update(at=None, value=None)


def _capacity(win, exporter, exclude=True):
    async def go():
        return await cap.compute_capacity(win, FlowFilter(exporter=exporter), ADMIN_VIS, EmptyDB(),
                                          settings=SETTINGS, exclude_provisional=exclude)

    return asyncio.run(go())


def _row(res, ifindex):
    return next(r for r in res.rows if r["ifindex"] == ifindex)


def test_provisional_slots_are_left_out_not_counted_as_idle(ch):
    """The acceptance repro: 1 kB keepalives on ifIndex 1 every minute for 70
    minutes while a 50 Mbps flow of the last 30 minutes is still active (not
    exported). The exporter's active timeout shows as a 30-minute span it
    reported earlier. The newest slots must not be idle zeros with full
    coverage; once the flow is reported, the settled samples are unchanged."""
    client, db = ch
    e = "198.18.220.11"
    now = C.now_utc().replace(microsecond=0)
    rows = [rec(now - timedelta(minutes=m), e, 1, 0, 1000) for m in range(70, 0, -1)]
    # Evidence: a 30-minute flow reported 50 minutes ago (the active timeout) on another interface.
    rows.append(rec(now - timedelta(minutes=50), e, 9, 0, 1000, first=now - timedelta(minutes=80)))
    client.insert(f"{db}.flow_records", rows, column_names=COLS)
    win = Window(start=now - timedelta(hours=1), end=now, mode="relative", tz="UTC")
    before = _capacity(win, e)
    r = _row(before, 1)
    settled = datetime.fromisoformat(r["settled_until"].replace("Z", "+00:00"))
    # 30 min (the reported span) + the export margin before the call, on a slot boundary.
    assert cap.settle_at(cap.SettleInfo(1800, "observed"), now) <= settled \
        <= cap.settle_at(cap.SettleInfo(1800, "observed"), C.now_utc())
    expected = int((settled - before.sw.first).total_seconds()) // 300
    assert r["expected_slots"] == expected < before.expected, "the settling tail is not expected yet"
    assert r["samples"] == expected and r["coverage_pct"] == 100.0
    assert [p["exporter"] for p in before.provisional] == [e] and before.provisional[0]["basis"] == "observed"
    assert before.provisional[0]["active_timeout_s"] == 1800
    # Without the exclusion (interface alerts keep their own settle delay) every slot counts.
    assert _row(_capacity(win, e, exclude=False), 1)["samples"] == before.expected
    # The long flow is reported now: its bytes go back into the provisional slots only.
    start = now - timedelta(minutes=30)
    cap._settle_cache.update(at=None, value=None)
    client.insert(f"{db}.flow_records", [rec(now, e, 1, 0, 50e6 * 1800 / 8, first=start)], column_names=COLS)
    after = _row(_capacity(win, e), 1)
    assert after["samples"] == r["samples"] and after["in"]["avg_bps"] == pytest.approx(r["in"]["avg_bps"])
    assert after["in"]["max_bps"] == pytest.approx(r["in"]["max_bps"]), "settled samples do not move"
    # A window that has settled entirely is not cut.
    old = Window(start=now - timedelta(hours=3), end=now - timedelta(hours=2), mode="fixed", tz="UTC")
    assert _capacity(old, e).provisional == []


def test_exporter_without_durations_assumes_the_default_timeout(ch):
    client, db = ch
    e = "198.18.220.12"
    now = C.now_utc().replace(microsecond=0)
    client.insert(f"{db}.flow_records", [rec(now - timedelta(minutes=m), e, 1, 0, 1000) for m in range(70, 0, -1)],
                  column_names=COLS)
    res = _capacity(Window(start=now - timedelta(hours=1), end=now, mode="relative", tz="UTC"), e)
    p = res.provisional[0]
    assert p["basis"] == "default" and p["active_timeout_s"] == cap.SETTLE_DEFAULT_S
    assert _row(res, 1)["expected_slots"] < res.expected


class MapDB(AuditDB):
    """Answers by SQL substring (audit rows, override rows, ...)."""

    def __init__(self, answers):
        super().__init__([])
        self.answers = answers

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        rows = next((v for k, v in self.answers.items() if k in sql), [])
        return SimpleNamespace(all=lambda: list(rows))


def test_trend_days_use_the_speed_in_effect_and_history_has_values(ch):
    """FE-05 through the real trend: an asymmetric 50/20 Mbps override set on
    the second day; each day's utilization uses that day's speeds and the
    history entry carries both directions' values."""
    client, db = ch
    e = "127.0.0.83"
    today = C.now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=3)
    rows = []
    for k in range(3 * 288):
        t = start + timedelta(minutes=5 * k, seconds=40)
        rows.append(rec(t, e, 1, 0, 10e6 * 300 / 8))       # 10 Mbps in
        rows.append(rec(t, e, 0, 1, 10e6 * 300 / 8))       # 10 Mbps out
    client.insert(f"{db}.flow_records", rows, column_names=COLS)
    changed = start + timedelta(days=1, hours=1)
    dbx = MapDB({
        "audit_logs": [(datetime.now(UTC), "netflow.interface_override.update", "netflow_interface_override",
                        {"changes": {"speed_out_bps": {"old": 10_000_000, "new": 20_000_000}}}, "admin"),
                       (changed, "netflow.interface_override.create", "netflow_interface_override",
                        {"changes": {"speed_in_bps": {"old": None, "new": 50_000_000},
                                     "speed_out_bps": {"old": None, "new": 10_000_000}}}, "admin")],
        "netflow_interface_overrides": [(e, 1, "Branch-3 DSL", 50_000_000, 20_000_000, "wan", None, None, None)],
    })

    async def go():
        w = Window(start=start, end=C.now_utc().replace(microsecond=0), mode="relative", tz="UTC")
        return await cap.interface_trend(w, e, 1, ADMIN_VIS, dbx, settings=SETTINGS)

    t = asyncio.run(go())
    days = {d["day"]: d for d in t["days"]}
    d0, d1, d2 = (days[(start + timedelta(days=i)).date().isoformat()] for i in range(3))
    assert d0["speed"]["in_bps"] is None and d0["in"]["utilization_p_pct"] is None      # no speed yet
    assert d1["speed"]["changed"] and (d1["speed"]["in_bps"], d1["speed"]["out_bps"]) == (50_000_000, 10_000_000)
    assert d2["in"]["utilization_p_pct"] == pytest.approx(20.0) and d2["out"]["utilization_p_pct"] == pytest.approx(100.0)
    # Today is measured against the current speeds, even for a change made within the current second.
    today_row = days[today.date().isoformat()]
    assert (today_row["speed"]["in_bps"], today_row["speed"]["out_bps"]) == (50_000_000, 20_000_000)
    assert t["speed"]["in_bps"] == 50_000_000 and t["speed"]["out_bps"] == 20_000_000
    newest, created = t["speed_history"]
    assert newest["in"] is None and newest["out"] == {"old": 10_000_000, "new": 20_000_000}
    assert created["in"] == {"old": None, "new": 50_000_000} and created["out"] == {"old": None, "new": 10_000_000}
    assert "_at" not in created


def test_lag_check_through_real_sql_reads_no_raw_until_suspected(ch, monkeypatch):
    client, db = ch
    e = "198.18.220.13"
    now = C.now_utc().replace(microsecond=0)
    start = floor_to(now - timedelta(hours=2), 300)
    client.insert(f"{db}.flow_records", [rec(start + timedelta(minutes=5 * k, seconds=100), e, 1, 0, 1e6)
                                         for k in range(12)]
                  + [rec(now - timedelta(minutes=m), e, 0, 0, 100) for m in range(1, 5)],     # keepalives
                  column_names=COLS)
    labels: list[str] = []
    real = cap.run_query

    async def spy(sql, params=None, **kw):
        labels.append(kw.get("label", ""))
        return await real(sql, params, **kw)

    monkeypatch.setattr(cap, "run_query", spy)
    sw = cap.complete_slots(start, now)
    # Interface rows stop an hour ago, keepalives (no interface) continue: not a lag.
    assert asyncio.run(cap.rollup_lag(sw, ADMIN_VIS)) is None
    assert labels.count("capacity-lag-probe") == 1
    # The rollup view stops, interface records continue: the probe finds them.
    client.command(f"DROP VIEW {db}.flow_interface_5m_mv")
    client.insert(f"{db}.flow_records", [rec(now - timedelta(minutes=m), e, 1, 0, 1e6) for m in range(1, 4)],
                  column_names=COLS)
    cap._lag_probe_cache.clear()
    lag = asyncio.run(cap.rollup_lag(sw, ADMIN_VIS))
    assert lag == start + timedelta(minutes=60), lag


# ── Live API (own uvicorn of this tree) ──────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
live = pytest.mark.skipif(not LIVE, reason="set NETFLOW_TEST_API to a running API of this tree")


@pytest.fixture(scope="module")
def live_auth():
    r = requests.post(f"{LIVE}/api/v1/auth/login", json={
        "username": os.environ.get("NETFLOW_TEST_USER", "admin"),
        "password": os.environ.get("NETFLOW_TEST_PASS", "admin123"),
    }, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@live
def test_live_capacity_window_discloses_provisional_and_interfaces_have_no_util(live_auth):
    cap_r = requests.get(f"{LIVE}/api/v1/netflow/capacity", params={"hours": 1, "envelope": 1, "limit": 5},
                         headers=live_auth, timeout=120).json()
    assert "provisional" in cap_r["data"]["window"]
    ifaces = requests.get(f"{LIVE}/api/v1/netflow/interfaces", params={"hours": 24, "with_totals": 1, "envelope": 1},
                          headers=live_auth, timeout=120).json()["data"]
    assert ifaces["utilization"]["source"] == "capacity"
    assert all("in_util_avg_pct" not in i and "out_util_avg_pct" not in i for i in ifaces["items"])


@live
def test_live_link_utilization_names_interface_exporters(live_auth):
    rows = requests.get(f"{LIVE}/api/v1/link-utilization", params={"hours": 24, "limit": 200},
                        headers=live_auth, timeout=120).json()
    items = rows.get("items", rows) if isinstance(rows, dict) else rows
    nf = [r for r in items if r.get("has_netflow")]
    if not nf:
        pytest.skip("no link with NetFlow on this stack")
    r = nf[0]
    d = requests.get(f"{LIVE}/api/v1/link-utilization/{r['device_id']}/{r['if_index']}", params={"hours": 24},
                     headers=live_auth, timeout=120).json()
    ie = d["netflow"]["interface_exporters"]
    assert isinstance(ie, list)
    assert all(x["exporter"] in d["netflow"]["exporters"] for x in ie)
    assert [x["bytes"] for x in ie] == sorted((x["bytes"] for x in ie), reverse=True)
