"""Device section pack (report_packs/devices.py) with canned data.

Postgres and ClickHouse are fakes answering by SQL substring; nothing here
connects to a database. Covers availability by group/site (sample weighting),
latency/loss/jitter (answered-probe averages, table choice, prior period),
flapping (state changes and paired outage episodes), reboots (sysUpTime
resets, counter wraps), planned maintenance and per-site reachability, plus
the device filter, options and organisation-timezone formatting.
"""
from __future__ import annotations

import asyncio
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from app.services import report_data_service as rds
from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import devices as pack

A, B, C, D = ("aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b",
              "cccccccc-0000-0000-0000-00000000000c", "dddddddd-0000-0000-0000-00000000000d")
NAMES = {A: "core-sw1", B: "core-sw2", C: "edge-rtr1", D: "printer-3f"}
TO = datetime(2026, 10, 7, 12, 0)          # naive UTC, like the render API
FRM = TO - timedelta(days=7)
MaintRow = namedtuple("MaintRow", "device_id s e")


# ── fakes ───────────────────────────────────────────────────────────────────

class _Rows:
    rowcount = 0

    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeDB:
    """Answers with the rows of the first route whose key occurs in the SQL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for key, rows in self.routes:
            if key in sql:
                return _Rows(rows(sql, params) if callable(rows) else rows)
        return _Rows([])

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()


class _CHResult:
    def __init__(self, rows):
        self.result_rows = list(rows)


class FakeCH:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        for key, rows in self.routes:
            if key in sql:
                return _CHResult(rows(sql, parameters) if callable(rows) else rows)
        return _CHResult([])

    def sql_with(self, needle: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if needle in c[0]]


def run(monkeypatch, db, ch, entry, frm=FRM, to=TO, device_ids=None):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    # Synchronous helpers (device_outage_episodes) use the module client.
    monkeypatch.setattr(rds, "get_ch_client", lambda: ch)
    entries = [entry if isinstance(entry, dict) else {"id": entry}]
    out = asyncio.run(rs.build_sections(db, entries, frm, to,
                                        {"device_ids": device_ids} if device_ids else None,
                                        requester=None, branding=Branding(timezone="Asia/Riyadh")))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        assert all(len(r) == len(t["headers"]) for r in t["rows"])
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)
    return sec


def kpi(sec, label_start):
    return next(k for k in sec["kpis"] if k["label"].startswith(label_start))


def inventory_rows(*ids, extra=()):
    return [(i, NAMES[i], f"10.0.0.{n}", *extra) for n, i in enumerate(ids, 1)]


def now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# ── availability_by_group ───────────────────────────────────────────────────

def _group_db(labels_key="LEFT JOIN device_groups g", labels=None):
    return FakeDB([
        ("SELECT id::text, hostname, ip_address::text", [
            (A, NAMES[A], "10.0.0.1", "DC1", "up"), (B, NAMES[B], "10.0.0.2", "DC1", "down"),
            (C, NAMES[C], "10.0.0.3", "Branch", "up"), (D, NAMES[D], "10.0.0.4", "", "unknown")]),
        ("FROM device_maintenance m", []),
        ("system_settings", [(99.5,)]),
        (labels_key, labels if labels is not None else
         [(A, "Core"), (B, "Core"), (C, "Edge"), (D, None)]),
    ])


def _group_ch():
    b = datetime(2026, 10, 5)
    return FakeCH([
        ("up_weighted", [(A, b, 990.0, 1000, 5.0), (B, b, 100.0, 100, 3.0),
                         (C, b, 1000.0, 1000, 10.0)]),
        ("node_samples", [(A, 1000), (B, 100), (C, 1000)]),
    ])


def test_availability_by_group_weights_by_samples_not_mean_of_percentages(monkeypatch):
    sec = run(monkeypatch, _group_db(), _group_ch(), "availability_by_group")
    assert sec["title"] == "Availability by Device Group"
    rows = {r[0]: r for r in sec["tables"][0]["rows"]}
    # Core: (990 + 100) / (1000 + 100) = 99.09%, not (99 + 100) / 2 = 99.50%.
    assert rows["Core"][1:4] == ["2", "2", "99.09%"]
    assert rows["Core"][6] == "1"                      # core-sw2 is down now
    assert rows["Core"][7] == "core-sw1 (99.00%)"
    assert rows["Edge"][3] == "100.00%"
    # The unmeasured node is "not measured", never 0 % or 100 %.
    assert rows["Ungrouped"][2:5] == ["0", "—", "—"]
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["Core", "Edge", "Ungrouped"]
    assert kpi(sec, "Lowest group")["value"] == "99.09%"
    assert kpi(sec, "Lowest group")["subtitle"] == "Core"
    assert kpi(sec, "Below target (99.5%)")["value"] == "1"
    assert kpi(sec, "Nodes not measured")["value"] == "1"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "Core", "value": 99.09}
    assert any("weighted by ping samples" in n for n in sec["notes"])


def test_availability_by_group_by_site_truncates_and_honours_device_filter(monkeypatch):
    db = _group_db("device_polling_owner", [(A, "Riyadh DC"), (B, "Controller"),
                                            (C, "Controller"), (D, "Controller")])
    ch = _group_ch()
    sec = run(monkeypatch, db, ch, {"id": "availability_by_group",
                                    "options": {"group_by": "site", "top_n": 1}},
              device_ids=[A, B])
    assert sec["title"] == "Availability by Monitoring Site"
    table = sec["tables"][0]
    assert table["title"] == "By monitoring site — top 1 of 2 monitoring sites"
    assert len(table["rows"]) == 1
    assert any("primary poller" in n for n in sec["notes"])
    # The device filter reaches Postgres and every ClickHouse query.
    assert any(p.get("dev_ids") == [A, B] for _s, p in db.calls if "device_polling_owner" in _s)
    assert ch.sql_with("node_samples")[0][1]["dev_ids"] == [A, B]
    assert ch.sql_with("up_weighted")[0][1]["ids"] == [A, B]


def test_availability_by_group_by_location_uses_node_location(monkeypatch):
    sec = run(monkeypatch, _group_db(), _group_ch(),
              {"id": "availability_by_group", "options": {"group_by": "location"}})
    names = [r[0] for r in sec["tables"][0]["rows"]]
    assert names == ["DC1", "Branch", "No location"]
    assert sec["tables"][0]["rows"][0][3] == "99.09%"


# ── device_latency_loss ─────────────────────────────────────────────────────

def _latency_db(maint=()):
    return FakeDB([("FROM device_maintenance m", list(maint)),
                   ("FROM devices d WHERE TRUE", inventory_rows(A, B, C, D))])


def _latency_ch():
    t1, t2 = datetime(2026, 10, 6, 0, 0), datetime(2026, 10, 6, 2, 0)
    return FakeCH([
        # did, samples, up, rtt_w, jit_w, loss_w, p95, peak
        ("GROUP BY did", [(A, 1000, 1000, 5000.0, 500.0, 20.0, 12.0, 40.0),
                          (B, 1000, 900, 18000.0, 1800.0, 108.1, 35.0, 80.0),
                          (C, 500, 0, 0.0, 0.0, 500.0, None, 0.0)]),
        ("GROUP BY b", [(t1, 1000, 1000, 6000.0, 600.0, 10.0), (t2, 10, 0, 0.0, 0.0, 10.0)]),
        ("timestamp < %(t)s", [(2000, 2000, 20000.0, 2000.0, 10.0)]),
    ])


def test_latency_loss_ranks_by_p95_and_averages_answered_probes(monkeypatch):
    ch = _latency_ch()
    sec = run(monkeypatch, _latency_db(), ch, "device_latency_loss")
    assert sec["title"] == "Top 10 Nodes by Latency (p95)"
    rows = sec["tables"][0]["rows"]
    assert rows == [
        [NAMES[B], "10.0.0.2", "20.0 ms", "35.0 ms", "80.0 ms", "2.00 ms", "0.90%"],
        [NAMES[A], "10.0.0.1", "5.00 ms", "12.0 ms", "40.0 ms", "0.50 ms", "2.00%"],
    ]
    # 7-day window -> 5-minute rollup, uptime-weighted sums.
    assert ch.sql_with("FROM zenplus.ping_metrics_5m")
    assert "sum(uptime_pct * sample_count)" in ch.sql_with("GROUP BY did")[0][0]
    assert kpi(sec, "Average latency")["value"] == "12.1 ms"
    assert kpi(sec, "Average latency")["subtitle"] == "▲ 2.11 ms vs previous period"
    assert kpi(sec, "Worst p95 latency")["value"] == "35.0 ms"
    assert kpi(sec, "Worst p95 latency")["subtitle"] == NAMES[B]
    assert kpi(sec, "Average packet loss")["value"] == "1.48%"
    assert kpi(sec, "Average packet loss")["subtitle"] == "▲ 0.98 pts vs previous period"
    assert kpi(sec, "Nodes with loss ≥ 1%")["value"] == "1"
    assert kpi(sec, "Average jitter")["value"] == "1.21 ms"
    assert kpi(sec, "Nodes measured")["value"] == "2"
    rank, trend = sec["charts"]
    assert rank["series"]["kind"] == "rank" and rank["series"]["unit"] == "ms"
    assert rank["series"]["points"][0] == {"label": NAMES[B], "value": 35.0}
    assert trend["series"]["points"] == [{"t": "2026-10-06T00:00:00Z", "v": 6.0},
                                         {"t": "2026-10-06T02:00:00Z", "v": None}]
    assert any("1 node(s) in scope have no ping samples" in n for n in sec["notes"])
    assert any("answered no probe" in n for n in sec["notes"])


def test_latency_loss_metric_switch_and_top_n(monkeypatch):
    sec = run(monkeypatch, _latency_db(), _latency_ch(),
              {"id": "device_latency_loss", "options": {"metric": "loss", "top_n": 1}})
    assert sec["title"] == "Top 1 Nodes by Packet Loss"
    table = sec["tables"][0]
    assert table["title"] == "Nodes by packet loss — top 1 of 2 nodes"
    assert [r[0] for r in table["rows"]] == [NAMES[A]]
    assert sec["charts"][0]["series"]["unit"] == "%"
    assert sec["charts"][1]["series"]["points"][0]["v"] == 1.0

    sec = run(monkeypatch, _latency_db(), _latency_ch(),
              {"id": "device_latency_loss", "options": {"metric": "jitter"}})
    assert [r[0] for r in sec["tables"][0]["rows"]] == [NAMES[B], NAMES[A]]


def test_latency_loss_picks_table_by_window_and_honours_filter_and_maintenance(monkeypatch):
    ch = _latency_ch()
    run(monkeypatch, _latency_db(), ch, "device_latency_loss", frm=TO - timedelta(days=30))
    assert ch.sql_with("FROM zenplus.ping_metrics_1h")

    ch = _latency_ch()
    maint = [MaintRow(A, datetime(2026, 10, 7, 9), datetime(2026, 10, 7, 10)),
             MaintRow(D, datetime(2026, 10, 7, 9), datetime(2026, 10, 7, 10))]
    run(monkeypatch, _latency_db(maint), ch, "device_latency_loss",
        frm=TO - timedelta(hours=3), device_ids=[A, B])
    sql, params = ch.sql_with("GROUP BY did")[0]
    assert "FROM zenplus.ping_metrics WHERE" in sql and "countIf(is_up = 1)" in sql
    assert params["dev_ids"] == [A, B]
    # Maintenance of a node outside the filter is not even mentioned.
    assert "AND NOT ((toString(device_id) IN %(mids0)s" in sql
    assert params["mids0"] == [A]


def test_latency_loss_reports_not_measured_without_samples(monkeypatch):
    sec = run(monkeypatch, _latency_db(), FakeCH([]), "device_latency_loss")
    assert kpi(sec, "Average latency")["value"] == "—"
    assert kpi(sec, "Average packet loss")["value"] == "—"
    assert sec["tables"] == []
    assert any("No node answered" in n for n in sec["notes"])


# ── device_flapping ─────────────────────────────────────────────────────────

def _flap_ch():
    b1, b2 = datetime(2026, 10, 6, 20), datetime(2026, 10, 6, 22)
    t = lambda h, m=0: datetime(2026, 10, 6, h, m)  # noqa: E731
    return FakeCH([
        ("WITH lower(old_status)", [(A, b1, 3, 2, 0, t(21, 30)), (A, b2, 3, 1, 1, t(22, 0)),
                                    (B, b2, 1, 1, 0, t(23, 0))]),
        ("argMax(old_status, timestamp)", []),
        ("ORDER BY did, timestamp", [
            (A, t(20, 0), "up", "down"), (A, t(20, 10), "down", "up"),
            (A, t(21, 0), "up", "down"), (A, t(21, 30), "down", "up"),
            (B, t(23, 0), "up", "down")]),
        ("min(timestamp)", []),
    ])


def test_flapping_counts_changes_pairs_outages_and_formats_in_org_timezone(monkeypatch):
    db = FakeDB([("FROM device_maintenance m", []),
                 ("FROM devices d WHERE TRUE", inventory_rows(A, B, C))])
    sec = run(monkeypatch, db, _flap_ch(), "device_flapping")
    rows = sec["tables"][0]["rows"]
    assert rows[0] == [NAMES[A], "6", "2", "1", "40 min", "30 min", "20 min", "Oct 07 01:00"]
    assert rows[1][:3] == [NAMES[B], "1", "1"]
    assert rows[1][5].endswith("(ongoing)") and rows[1][6] == "—"
    assert kpi(sec, "Flapping nodes (≥ 4 changes)")["value"] == "1"
    assert kpi(sec, "State changes")["value"] == "7"
    assert kpi(sec, "Outage episodes")["value"] == "3"
    assert kpi(sec, "Median time to recover")["value"] == "20 min"
    assert kpi(sec, "Longest outage")["subtitle"] == NAMES[B]
    bars, hist = sec["charts"]
    assert bars["series"]["kind"] == "bars"
    assert [p["v"] for p in bars["series"]["points"]] == [3, 4]
    assert {p["label"]: p["value"] for p in hist["series"]["points"]}["5–30 min"] == 1


def test_flapping_threshold_option_and_device_filter(monkeypatch):
    db = FakeDB([("FROM device_maintenance m", []),
                 ("FROM devices d WHERE TRUE", inventory_rows(A, B))])
    ch = _flap_ch()
    sec = run(monkeypatch, db, ch, {"id": "device_flapping", "options": {"threshold": 7, "top_n": 1}},
              device_ids=[A, B])
    assert kpi(sec, "Flapping nodes (≥ 7 changes)")["value"] == "0"
    assert sec["tables"][0]["title"] == "Nodes with state changes or outages — top 1 of 2 nodes"
    assert ch.sql_with("WITH lower(old_status)")[0][1]["dev_ids"] == [A, B]
    assert sorted(ch.sql_with("ORDER BY did, timestamp")[0][1]["ids"]) == [A, B]


def test_flapping_empty_period(monkeypatch):
    db = FakeDB([("FROM devices d WHERE TRUE", inventory_rows(A))])
    sec = run(monkeypatch, db, FakeCH([]), "device_flapping")
    assert sec["tables"] == []
    assert sec["notes"][0] == "No node changed state or had an outage in this period."
    assert kpi(sec, "Median time to recover")["value"] == "—"


# ── device_reboots ──────────────────────────────────────────────────────────

def _reboot_setup(to):
    frm = to - timedelta(days=7)
    first, last = frm + timedelta(hours=1), to - timedelta(hours=1)
    elapsed = (last - first).total_seconds()
    r1, r2, w = to - timedelta(days=5), to - timedelta(days=2), to - timedelta(days=3)
    db = FakeDB([("FROM devices d WHERE TRUE", inventory_rows(
        A, B, C, D, extra=("Cisco", "C9300")))])
    ch = FakeCH([
        ("argMin(", [(A, 1_000_000.0, 172_800.0, first, last),
                     (B, 500_000.0, 500_000.0 + elapsed, first, last),
                     (C, pack.SYSUPTIME_MAX_S - 1000, 600_000.0, first, last)]),
        ("lagInFrame", [(A, r1, 100.0, 1_050_000.0, r1 - timedelta(seconds=60)),
                        (A, r2, 30.0, 90_000.0, r2 - timedelta(seconds=60)),
                        (C, w, 25.0, pack.SYSUPTIME_MAX_S - 20, w - timedelta(seconds=60))]),
    ])
    return frm, db, ch, r2


def test_reboots_detects_resets_and_skips_counter_wraps(monkeypatch):
    to = now_naive()
    frm, db, ch, r2 = _reboot_setup(to)
    sec = run(monkeypatch, db, ch, "device_reboots", frm=frm, to=to)
    # Raw history for a recent window, two passes, flagged nodes only in pass 2.
    assert ch.sql_with("FROM zenplus.snmp_metrics WHERE")
    assert ch.sql_with("lagInFrame")[0][1]["flagged"] == [A, C]
    rows = sec["tables"][0]["rows"]
    boot = Branding(timezone="Asia/Riyadh").fmt_short(r2 - timedelta(seconds=30))
    assert rows == [[NAMES[A], "10.0.0.1", "Cisco C9300", "2", boot, "2 d 0 h"]]
    assert kpi(sec, "Nodes rebooted")["value"] == "1"
    assert kpi(sec, "Reboots detected")["value"] == "2"
    assert kpi(sec, "Most reboots")["subtitle"] == NAMES[A]
    assert kpi(sec, "Nodes measured")["value"] == "3"
    assert any("counter wrap" in n for n in sec["notes"])
    assert any("1 node(s) reported no SNMP uptime" in n for n in sec["notes"])
    assert sum(p["v"] for p in sec["charts"][0]["series"]["points"]) == 2


def test_reboots_use_5m_rollup_beyond_raw_retention_and_filter(monkeypatch):
    to = now_naive() - timedelta(days=40)
    frm, db, ch, _r2 = _reboot_setup(to)
    sec = run(monkeypatch, db, ch, "device_reboots", frm=to - timedelta(days=50), to=to,
              device_ids=[A])
    sql, params = ch.sql_with("argMin(")[0]
    assert "FROM zenplus.snmp_metrics_5m" in sql and "argMin(min_value, timestamp)" in sql
    assert params["dev_ids"] == [A]
    assert "min_value AS cur" in ch.sql_with("lagInFrame")[0][0]
    assert any("5-minute resolution" in n for n in sec["notes"])


# ── maintenance_summary ─────────────────────────────────────────────────────

def _maint_db():
    utc = timezone.utc
    w1 = ("w1", "group", None, datetime(2026, 10, 3, 22, tzinfo=utc), datetime(2026, 10, 4, 2, tzinfo=utc),
          "Firmware upgrade", None, "Core switches", 2)
    w2 = ("w2", "all", None, datetime(2026, 9, 29, 0, tzinfo=utc), datetime(2026, 9, 30, 13, tzinfo=utc),
          "", None, None, 3)
    s1, e1 = datetime(2026, 10, 3, 22, tzinfo=utc), datetime(2026, 10, 4, 2, tzinfo=utc)
    s2, e2 = datetime(2026, 9, 30, 12, tzinfo=utc), datetime(2026, 9, 30, 13, tzinfo=utc)
    return FakeDB([
        ("COUNT(DISTINCT d.id)", [w2, w1]),
        ("GREATEST(m.starts_at", [(A, s1, e1), (B, s1, e1), (A, s2, e2), (B, s2, e2), (C, s2, e2)]),
    ])


def test_maintenance_summary_labels_scopes_and_merges_excluded_time(monkeypatch):
    db = _maint_db()
    sec = run(monkeypatch, db, FakeCH([]), "maintenance_summary", device_ids=[A, B, C])
    rows = sec["tables"][0]["rows"]
    assert [r[0] for r in rows] == ["All devices", "Group: Core switches"]
    w2, w1 = rows
    assert w1[1:6] == ["2026-10-04 01:00", "2026-10-04 05:00", "4 h 00 min", "2", "8.0"]
    assert w1[6] == "Firmware upgrade" and w1[7] in ("completed", "active", "scheduled")
    assert w2[5] == "3.0" and w2[6] == "—"          # clamped to the period start
    assert kpi(sec, "Maintenance windows")["value"] == "2"
    assert kpi(sec, "Nodes covered")["value"] == "3"
    assert kpi(sec, "Device-hours excluded")["value"] == "11.0 h"
    assert kpi(sec, "Longest window")["value"] == "1 d 13 h"
    assert kpi(sec, "Longest window")["subtitle"] == "All devices"
    assert all(p.get("dev_ids") == [A, B, C] for _s, p in db.calls)


def test_maintenance_summary_empty(monkeypatch):
    sec = run(monkeypatch, FakeDB([]), FakeCH([]), "maintenance_summary")
    assert sec["tables"] == []
    assert "No planned maintenance" in sec["notes"][0]


# ── site_vantage_reachability ───────────────────────────────────────────────

SENSOR = "5e5e5e5e-0000-0000-0000-000000000001"


def _vantage(to):
    last = to - timedelta(minutes=1)
    db = FakeDB([("FROM device_maintenance m", []),
                 ("FROM devices d WHERE TRUE", inventory_rows(A, B)),
                 ("FROM sensors s", [(SENSOR, "ryd-sensor-01", "Riyadh DC")])])
    ch = FakeCH([("GROUP BY did, poller_id", [
        (A, "poller-01", 1000, 1000, 5000.0, 1000, 0.0, last),
        (A, SENSOR, 1000, 950, 28500.0, 950, 9.5, last),
        (B, "poller-01", 1000, 1000, 2000.0, 1000, 0.0, last)])])
    return db, ch


def test_site_vantage_compares_sites(monkeypatch):
    monkeypatch.delenv("POLLER_ID", raising=False)
    to = now_naive()
    db, ch = _vantage(to)
    sec = run(monkeypatch, db, ch, "site_vantage_reachability", frm=to - timedelta(days=7), to=to)
    sites, nodes = sec["tables"]
    assert sites["rows"][0][:6] == ["Riyadh DC", "ryd-sensor-01", "1", "95.00%", "30.0 ms", "1.00%"]
    assert sites["rows"][1][:4] == ["Controller", "Central poller", "2", "100.00%"]
    assert nodes["rows"] == [[NAMES[A], "Controller", "100.00%", "Riyadh DC · ryd-sensor-01",
                              "95.00%", "5.00 pts", "5.00 ms – 30.0 ms"]]
    assert kpi(sec, "Monitoring sites")["value"] == "2"
    assert kpi(sec, "Nodes seen from 2+ sites")["value"] == "1"
    assert kpi(sec, "Lowest site availability")["value"] == "95.00%"
    assert kpi(sec, "Lowest site availability")["subtitle"] == "Riyadh DC · ryd-sensor-01"


def test_site_vantage_threshold_retention_and_filter(monkeypatch):
    monkeypatch.delenv("POLLER_ID", raising=False)
    to = now_naive()
    db, ch = _vantage(to)
    frm = to - timedelta(days=45)
    sec = run(monkeypatch, db, ch, {"id": "site_vantage_reachability", "options": {"spread": 10}},
              frm=frm, to=to, device_ids=[A, B])
    assert len(sec["tables"]) == 1
    assert "agree within 10 percentage points" in sec["notes"][0]
    params = ch.sql_with("GROUP BY did, poller_id")[0][1]
    assert params["dev_ids"] == [A, B]
    assert params["f"] > frm                       # clamped to raw retention
    assert any("kept for 30 days" in n for n in sec["notes"])

    old = run(monkeypatch, *_vantage(to), "site_vantage_reachability",
              frm=to - timedelta(days=70), to=to - timedelta(days=40))
    assert "kept for 30 days" in old["notes"][0] and old["tables"] == []


# ── helpers, metadata and templates ─────────────────────────────────────────

def test_maintenance_clause_shares_one_term_per_interval():
    s, e = datetime(2026, 10, 1), datetime(2026, 10, 2)
    sql, params = pack._maint_sql({A: [(s, e)], B: [(s, e)], C: [(e, e + timedelta(hours=1))]})
    assert sql.count("toString(device_id) IN") == 2
    assert params["mids0"] == [A, B]


def test_formatting_helpers():
    assert pack._ms(0.4211) == "0.42 ms" and pack._ms(12.34) == "12.3 ms"
    assert pack._ms(245.4) == "245 ms" and pack._ms(1250) == "1.25 s" and pack._ms(None) == "—"
    assert pack._duration(42) == "42 s" and pack._duration(3 * 3600 + 300) == "3 h 05 min"
    assert pack._duration(None) == "—"
    assert pack._change(None, 1.0, " ms") == "Not measured"
    assert pack._change(2.0, None, " ms") == "No data for the previous period"
    assert pack._change(2.0, 2.001, " ms") == "No change vs previous period"
    assert pack._change(1.0, 3.5, " pts") == "▼ 2.50 pts vs previous period"
    assert pack._is_wrap(pack.SYSUPTIME_MAX_S - 30, 30.0, 60.0)
    assert not pack._is_wrap(1_000_000.0, 30.0, 60.0)


PACK_IDS = ("availability_by_group", "device_latency_loss", "device_flapping", "device_reboots",
            "maintenance_summary", "site_vantage_reachability")
TEMPLATE_KEYS = ("availability_by_site_group", "node_availability_previous_month",
                 "down_degraded_nodes", "latency_loss_jitter", "top_nodes_response_time",
                 "unstable_devices_flapping", "device_reboot_report", "planned_maintenance_log",
                 "reachability_by_monitoring_site")


@pytest.mark.parametrize("sid", PACK_IDS)
def test_sections_are_device_scoped_and_need_devices_view(sid):
    e = rs.SECTION_REGISTRY[sid]
    assert e["scope"] == "device" and e["requires"] == ("devices.view",)
    assert sid in rs.SCOPE_SAFE_SECTIONS


def test_templates_are_registered_valid_and_published():
    assert rt.validate_catalog() == []
    for key in TEMPLATE_KEYS:
        t = rt.TEMPLATES[key]
        assert t.category in ("availability", "performance")
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
        assert "devices" in rt.catalog_entry(t)["filterable"]
    assert rt.TEMPLATES["node_availability_previous_month"].section_ids == [
        "availability_kpis", "availability_trend", "device_availability", "top_outages",
        "maintenance_summary"]
