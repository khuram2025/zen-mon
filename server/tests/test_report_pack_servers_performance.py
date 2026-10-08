"""servers_performance section pack: agent-monitored server CPU, memory, disk,
network, processes and filesystem forecast sections.

ClickHouse and Postgres are fakes answering canned rows by SQL substring; the
tests check the numbers, formatting ('—' when not measured, organisation
timezone), the option effects and the parameters each query receives."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import servers_performance as sp

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
SERVERS = [(A, "app-01", 8, 16_000_000_000), (B, "db-01", 4, 8_000_000_000), (C, "web-01", 2, None)]
TO = datetime(2026, 10, 7, 12, 0)
FRM = TO - timedelta(days=7)
NAN = float("nan")

PACK_IDS = ("server_cpu_change", "server_resource_utilization", "server_resource_trend",
            "server_saturation_hotspots", "server_memory_pressure", "server_disk_io",
            "server_network_throughput", "server_top_processes", "capacity_filesystem_forecast")
TEMPLATE_KEYS = ("server_cpu_increases", "server_top_cpu", "server_top_memory",
                 "server_resource_trend_review", "server_saturation_review",
                 "server_memory_swap_pressure", "server_disk_latency_utilization",
                 "server_network_throughput_review", "server_top_processes_review",
                 "server_disk_capacity_forecast")


class _Rows:
    def __init__(self, rows):
        self.result_rows = list(rows)


class FakeCH:
    """Answers the first rule whose substrings all occur in the SQL."""

    def __init__(self, rules=()):
        self.rules = list(rules)
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        for needles, rows in self.rules:
            if all(n in sql for n in needles):
                return _Rows(rows)
        return _Rows([])

    def find(self, *needles) -> tuple[str, dict]:
        hits = [c for c in self.calls if all(n in c[0] for n in needles)]
        assert hits, f"no query containing {needles}"
        return hits[-1]


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self, servers=SERVERS, filesystems=()):
        self.servers = list(servers)
        self.filesystems = list(filesystems)
        self.calls: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if "FROM servers s WHERE" in sql:
            return _Result(self.servers)
        if "server_filesystem_inventory" in sql:
            return _Result(self.filesystems)
        return _Result([])

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()


def run(monkeypatch, entries, ch=None, db=None, frm=FRM, to=TO):
    ch = ch or FakeCH()
    db = db or FakeDB()
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    entries = [e if isinstance(e, dict) else {"id": e} for e in entries]
    out = asyncio.run(rs.build_sections(db, entries, frm, to, None, requester=None,
                                        branding=Branding(timezone="Asia/Riyadh")))
    for sec in out:
        assert not sec.get("error"), sec["notes"]
        for t in sec["tables"]:
            assert len(t["styles"]) == len(t["headers"])
            for row in t["rows"]:
                assert len(row) == len(t["headers"]), row
                assert all(isinstance(c, str) for c in row), row
        for k in sec["kpis"]:
            assert isinstance(k["value"], str)
    return out, ch, db


def kpi(sec, label):
    for k in sec["kpis"]:
        if k["label"] == label:
            return k
    raise AssertionError(f"no KPI {label!r} in {[k['label'] for k in sec['kpis']]}")


# ── registration ────────────────────────────────────────────────────────────

def test_sections_and_templates_are_registered_for_servers_view():
    for sid in PACK_IDS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "fleet", sid
        assert e["requires"] == ("servers.view",), sid
        assert e["fn"].__module__ == sp.__name__
    assert rs.SECTION_REGISTRY["capacity_filesystem_forecast"]["category"] == "Capacity"
    assert rt.validate_catalog() == []
    for key in TEMPLATE_KEYS:
        t = rt.TEMPLATES[key]
        assert t.category in ("servers", "capacity")
        assert set(t.section_ids) <= set(rs.SECTION_REGISTRY)
        assert t.requires() == {"servers.view"}
        assert key in rs.REPORT_PRESETS
    assert rt.TEMPLATES["server_cpu_increases"].featured
    assert rt.TEMPLATES["server_disk_capacity_forecast"].featured


# ── server_cpu_change ───────────────────────────────────────────────────────

CPU_CHANGE_ROWS = [
    # server, s_now, n_now, s_before, n_before, p95_now, p95_before, peak_now
    (A, 60 * 400, 400, 30 * 400, 400, 72.5, 35.0, 99.0),
    (B, 50 * 500, 500, 49.5 * 500, 500, 55.0, 54.0, 60.0),
    (C, 80 * 100, 100, 0, 0, 82.0, NAN, 85.0),
]


def test_cpu_change_ranks_increases_and_marks_missing_baseline(monkeypatch):
    ch = FakeCH([(("sumIf(s, b >= %(f)s)",), CPU_CHANGE_ROWS)])
    db = FakeDB()
    (sec,), ch, db = run(monkeypatch, ["server_cpu_change"], ch, db)

    assert sec["title"] == "Biggest Server CPU Increases"
    sql, params = ch.find("host_cpu_metrics_5m", "quantileIf(0.95)")
    assert "sum(avg_total_pct * sample_count)" in sql  # weighted by sample_count
    assert params["sids"] == (A, B, C)
    assert params["f"] == "2026-09-30 12:00:00" and params["t"] == "2026-10-07 12:00:00"
    assert params["lo"] == "2026-09-23 12:00:00"  # previous equal window
    assert any("status <> 'disabled'" in q for q in db.calls)

    fleet = kpi(sec, "Fleet average CPU")
    assert fleet["value"] == "57.0%"
    assert fleet["subtitle"] == "▲ 16.17 pts vs previous period"
    assert kpi(sec, "Servers with higher CPU")["value"] == "1"
    assert kpi(sec, "Biggest increase")["value"] == "+30.00 pts"
    assert kpi(sec, "Biggest increase")["subtitle"] == "app-01"
    assert kpi(sec, "Servers measured")["value"] == "3 of 3"

    rows = sec["tables"][0]["rows"]
    assert rows == [
        ["app-01", "60.0%", "30.0%", "+30.00 pts", "72.5%", "35.0%", "99.0%"],
        ["web-01", "80.0%", "—", "—", "82.0%", "—", "85.0%"],
    ]
    assert sec["charts"][0]["series"]["kind"] == "rank"
    assert sec["charts"][0]["series"]["points"] == [{"label": "app-01", "value": 30.0}]
    # Previous period shown in the organisation timezone (UTC+3).
    assert "2026-09-23 15:00 – 2026-09-30 15:00 (GMT+3)" in sec["notes"][0]
    assert any("1 server(s) have no CPU samples in the previous period" in n for n in sec["notes"])


def test_cpu_change_min_change_and_top_n(monkeypatch):
    ch = FakeCH([(("sumIf(s, b >= %(f)s)",), CPU_CHANGE_ROWS)])
    (sec,), _, _ = run(monkeypatch, [{"id": "server_cpu_change", "options": {"min_change": 0}}], ch)
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["app-01", "db-01", "web-01"]
    assert sec["tables"][0]["rows"][1][3] == "+0.50 pts"

    ch = FakeCH([(("sumIf(s, b >= %(f)s)",), CPU_CHANGE_ROWS)])
    (sec,), _, _ = run(monkeypatch, [{"id": "server_cpu_change", "options": {"top_n": 1}}], ch)
    t = sec["tables"][0]
    assert t["title"] == "Top 1 of 2 servers by CPU increase"
    assert [r[0] for r in t["rows"]] == ["app-01"]


def test_cpu_change_without_samples_or_servers(monkeypatch):
    (sec,), ch, _ = run(monkeypatch, ["server_cpu_change"])
    assert sec["notes"][0] == "No server CPU samples in this period."
    assert kpi(sec, "Fleet average CPU")["value"] == "—"
    assert kpi(sec, "Biggest increase")["value"] == "—"
    assert sec["tables"] == [] and sec["charts"] == []
    assert any("3 enabled server(s) sent no CPU samples" in n for n in sec["notes"])

    (sec,), ch, _ = run(monkeypatch, ["server_cpu_change"], db=FakeDB(servers=[]))
    assert sec["notes"] == [sp.NO_SERVERS]
    assert ch.calls == []  # no enabled servers: ClickHouse is not queried


# ── server_resource_utilization ─────────────────────────────────────────────

def test_resource_utilization_ranks_by_p95_with_load_grades(monkeypatch):
    ch = FakeCH([(("host_cpu_metrics_5m",), [
        (A, 95 * 100, 100, 99.0, 100.0),
        (B, 40 * 100, 100, 50.0, 70.0),
        (C, 91 * 10, 10, 92.0, 95.0),
    ])])
    (sec,), ch, _ = run(monkeypatch, [{"id": "server_resource_utilization",
                                       "options": {"top_n": 2}}], ch)
    assert sec["title"] == "Top 2 Servers by CPU (95th Percentile)"
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 3 servers by 95th percentile CPU"
    assert t["headers"] == ["Server", "Avg CPU", "P95 CPU", "Peak CPU", "Cores", "Load"]
    assert t["styles"][2] == "pct-bar"
    assert t["rows"] == [["app-01", "95.0%", "99.0%", "100.0%", "8", "critical"],
                         ["web-01", "91.0%", "92.0%", "95.0%", "2", "warning"]]
    assert kpi(sec, "Fleet average CPU")["value"] == "68.6%"
    hot = kpi(sec, "Servers at or above 90% (P95)")
    assert hot["value"] == "2" and hot["accent"] == "danger"
    assert hot["subtitle"] == "1 at or above 98%"


def test_resource_utilization_metric_switch_to_memory(monkeypatch):
    ch = FakeCH([(("host_memory_metrics_5m",), [(A, 70 * 100, 100, 75.0, 80.0)])])
    (sec,), ch, _ = run(monkeypatch, [{"id": "server_resource_utilization",
                                       "options": {"metric": "memory", "sort": "avg"}}], ch)
    sql, _ = ch.find("host_memory_metrics_5m")
    assert "avg_used_pct * sample_count" in sql and "max(max_used_pct)" in sql
    assert not any("host_cpu_metrics_5m" in s for s, _ in ch.calls)
    assert sec["title"] == "Top 10 Servers by Memory (Average)"
    assert sec["tables"][0]["headers"][1] == "Avg memory"
    assert sec["tables"][0]["rows"] == [["app-01", "70.0%", "75.0%", "80.0%", "14.9 GB", "ok"]]
    assert kpi(sec, "Servers measured")["value"] == "1 of 3"
    assert any("2 enabled server(s) sent no memory samples" in n for n in sec["notes"])


# ── server_resource_trend ───────────────────────────────────────────────────

def test_resource_trend_multi_series_with_gaps_in_org_timezone(monkeypatch):
    t0 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    ch = FakeCH([
        (("toUnixTimestamp(toDateTime(", "host_cpu_metrics_5m"), [
            (t0, 50 * 10, 10, 70.0, 2),
            (t0 + 7200, 60 * 10, 10, 95.5, 2),
            (t0 + 4 * 7200, 40 * 10, 10, 45.0, 1),
        ]),
        (("uniqExact(server_id) FROM zenplus.host_cpu_metrics_5m",), [(2,)]),
    ])
    (sec,), ch, _ = run(monkeypatch, ["server_resource_trend"], ch)
    sql, params = ch.find("toUnixTimestamp(toDateTime(", "host_cpu_metrics_5m")
    assert "toStartOfInterval(b, INTERVAL 2 HOUR, %(org_tz)s)" in sql
    assert params["org_tz"] == "Asia/Riyadh"

    assert len(sec["charts"]) == 1  # memory had no samples: no empty chart
    series = sec["charts"][0]["series"]
    assert series["kind"] == "line" and series["unit"] == "%" and series["y_domain"] == [0, 100]
    assert [s["name"] for s in series["series"]] == ["Average", "95th percentile"]
    avg = series["series"][0]["points"]
    assert [p["v"] for p in avg] == [50.0, 60.0, None, 40.0]  # a gap stays a gap
    assert avg[0]["t"] == "2026-10-01T00:00:00Z" and avg[2]["t"] == "2026-10-01T04:00:00Z"
    assert [p["v"] for p in series["series"][1]["points"]] == [70.0, 95.5, None, 45.0]

    assert kpi(sec, "Fleet average CPU")["value"] == "50.0%"
    worst = kpi(sec, "Highest interval P95 CPU")
    assert worst["value"] == "95.5%" and worst["accent"] == "warning"
    assert worst["subtitle"] == "Oct 01 05:00"  # 02:00 UTC in Asia/Riyadh
    assert kpi(sec, "Fleet average memory")["value"] == "—"
    assert kpi(sec, "Highest interval P95 memory")["subtitle"] == "Not measured"
    assert kpi(sec, "Servers reporting")["value"] == "2 of 3"
    assert "2-hour interval (GMT+3)" in sec["notes"][0]


def test_resource_trend_empty(monkeypatch):
    (sec,), _, _ = run(monkeypatch, ["server_resource_trend"])
    assert sec["notes"] == ["No server CPU or memory samples in this period."]
    assert sec["charts"] == []


# ── server_saturation_hotspots ──────────────────────────────────────────────

def test_saturation_hotspots_minutes_runs_and_not_measured(monkeypatch):
    epoch = datetime(1970, 1, 1)
    ch = FakeCH([
        (("groupArrayIf", "host_cpu_metrics_5m"), [
            (A, 16, 2016, datetime(2026, 10, 6, 21, 0), 12),
            (B, 0, 2016, epoch, 0),
        ]),
        (("groupArrayIf", "host_memory_metrics_5m"), [
            (A, 6, 2016, datetime(2026, 10, 5, 3, 0), 6),
            (B, 2, 100, datetime(2026, 10, 2, 9, 0), 1),
            (C, 1, 50, datetime(2026, 10, 3, 9, 0), 1),
        ]),
    ])
    (sec,), ch, _ = run(monkeypatch, ["server_saturation_hotspots"], ch)
    _, params = ch.find("groupArrayIf", "host_cpu_metrics_5m")
    assert params["thr"] == 90.0
    assert sec["title"] == "Sustained Saturation Hotspots (≥90%)"
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["app-01", "1 h 20 min", "1 h", "30 min", "30 min", "Oct 07 00:00"]
    assert rows[1] == ["db-01", "0 min", "0 min", "10 min", "5 min", "Oct 02 12:00"]
    assert rows[2] == ["web-01", "—", "—", "5 min", "5 min", "Oct 03 12:00"]
    assert kpi(sec, "Servers with CPU ≥90%")["value"] == "1"
    assert kpi(sec, "Servers with CPU ≥90%")["subtitle"] == "of 2 measured"
    assert kpi(sec, "Servers with memory ≥90%")["value"] == "3"
    assert kpi(sec, "CPU-saturated time")["value"] == "1 h 20 min"
    longest = kpi(sec, "Longest run")
    assert longest["value"] == "1 h" and longest["subtitle"] == "app-01 · CPU"
    assert longest["accent"] == "danger"


def test_saturation_threshold_option_and_empty(monkeypatch):
    ch = FakeCH([(("groupArrayIf",), [(A, 0, 2016, datetime(1970, 1, 1), 0)])])
    (sec,), ch, _ = run(monkeypatch, [{"id": "server_saturation_hotspots",
                                       "options": {"threshold": 95}}], ch)
    assert all(p["thr"] == 95.0 for _, p in ch.calls)
    assert sec["title"] == "Sustained Saturation Hotspots (≥95%)"
    assert sec["tables"] == []
    assert sec["notes"][0].startswith("No server averaged 95% CPU or memory")


# ── server_memory_pressure ──────────────────────────────────────────────────

MEMORY_ROWS = [
    # server, avg, peak, swap avg, swap peak, swap n, commit avg, commit n, total, samples
    (A, 75.0, 92.0, 60.0, 70.0, 100, 125.0, 100, 16_000_000_000, 100),
    (B, 50.0, 55.0, NAN, 0.0, 0, NAN, 0, 8_000_000_000, 50),
    (C, 98.0, 99.0, 10.0, 12.0, 10, 80.0, 10, 0, 10),
]


def test_memory_pressure_swap_commit_and_status(monkeypatch):
    ch = FakeCH([(("FROM zenplus.host_memory_metrics ",), MEMORY_ROWS)])
    (sec,), ch, _ = run(monkeypatch, ["server_memory_pressure"], ch)
    sql, _ = ch.find("FROM zenplus.host_memory_metrics ")
    assert "swap_total_bytes > 0" in sql and "committed_bytes" in sql
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["web-01", "—", "98.0%", "99.0%", "10.0%", "12.0%", "80.0%", "critical"]
    assert rows[1] == ["app-01", "14.9 GB", "75.0%", "92.0%", "60.0%", "70.0%", "125.0%", "warning"]
    assert rows[2] == ["db-01", "7.5 GB", "50.0%", "55.0%", "—", "—", "—", "ok"]
    assert kpi(sec, "Fleet average memory used")["value"] == "68.6%"  # sample-weighted
    assert kpi(sec, "Servers peaking ≥90%")["value"] == "2"
    swap = kpi(sec, "Highest average swap use")
    assert swap["value"] == "60.0%" and swap["subtitle"] == "app-01" and swap["accent"] == "warning"
    assert kpi(sec, "Over-committed servers")["value"] == "1"


def test_memory_pressure_sort_by_swap_puts_unmeasured_last(monkeypatch):
    ch = FakeCH([(("FROM zenplus.host_memory_metrics ",), MEMORY_ROWS)])
    (sec,), _, _ = run(monkeypatch, [{"id": "server_memory_pressure",
                                      "options": {"sort": "swap", "top_n": 2}}], ch)
    t = sec["tables"][0]
    assert [r[0] for r in t["rows"]] == ["app-01", "web-01"]
    assert t["title"] == "Top 2 of 3 servers by swap used"


# ── server_disk_io ──────────────────────────────────────────────────────────

DISK_ROWS = [
    # server, device, avg util, p95 util, read ms, write ms, queue, iops, bytes/s, samples
    (A, "sda", 40.0, 70.0, 2.0, 4.5, 1.5, 150.0, 3_000_000.0, 100),
    (B, "nvme0n1", 10.0, 20.0, None, None, 0.1, 0.0, 0.0, 100),
]


def test_disk_io_latency_iops_and_formatting(monkeypatch):
    ch = FakeCH([(("host_disk_io_metrics",), DISK_ROWS)])
    (sec,), ch, _ = run(monkeypatch, ["server_disk_io"], ch)
    sql, params = ch.find("host_disk_io_metrics")
    assert "ORDER BY avg_util DESC" in sql
    assert "NOT match(device, '^(loop|ram)[0-9]')" in sql
    assert "sum(avg_read_ms * read_iops) / sum(read_iops)" in sql  # IOPS-weighted latency
    assert params["cap"] == sp.MAX_DISK_ROWS
    assert sec["title"] == "Top 15 Disks by Utilization"
    assert sec["tables"][0]["rows"] == [
        ["app-01", "sda", "40.0%", "70.0%", "2.00 ms", "4.50 ms", "1.50", "150.0", "2.9 MB/s"],
        ["db-01", "nvme0n1", "10.0%", "20.0%", "—", "—", "0.10", "0.0", "0.0 B/s"],
    ]
    assert kpi(sec, "Slowest disk (avg latency)")["value"] == "4.50 ms"
    assert kpi(sec, "Slowest disk (avg latency)")["subtitle"] == "app-01 · sda"
    assert kpi(sec, "Disks measured")["subtitle"] == "on 2 server(s)"


def test_disk_io_sort_option_and_truncation(monkeypatch):
    ch = FakeCH([(("host_disk_io_metrics",), DISK_ROWS)])
    (sec,), ch, _ = run(monkeypatch, [{"id": "server_disk_io",
                                       "options": {"sort": "latency", "top_n": 1}}], ch)
    sql, _ = ch.find("host_disk_io_metrics")
    assert "ORDER BY greatest(ifNull(read_ms, 0), ifNull(write_ms, 0)) DESC" in sql
    assert sec["title"] == "Top 1 Disks by Latency"
    assert sec["tables"][0]["title"] == "Top 1 of 2 disks by latency"
    assert sec["charts"][0]["series"]["unit"] == "ms"


# ── server_network_throughput ───────────────────────────────────────────────

NET_ROWS = [
    # server, avg rx B/s, avg tx B/s, peak B/s, errors/s, intervals with errors, ifs, buckets
    (A, 1_250_000.0, 250_000.0, 2_000_000.0, 0.0, 0, 2, 2016),
    (B, 125_000.0, 125_000.0, 300_000.0, 0.02, 202, 1, 2016),
]


def test_network_throughput_converts_bytes_to_bits(monkeypatch):
    ch = FakeCH([(("host_network_metrics_5m",), NET_ROWS)])
    (sec,), ch, _ = run(monkeypatch, ["server_network_throughput"], ch)
    sql, _ = ch.find("host_network_metrics_5m")
    assert "lower(if_name) != 'lo'" in sql
    assert "sum(avg_rx_bps * sample_count) / sum(sample_count)" in sql
    assert sec["tables"][0]["rows"] == [
        ["app-01", "2", "10.0 Mbps", "2.0 Mbps", "12.0 Mbps", "16.0 Mbps", "0.000", "0"],
        ["db-01", "1", "1.0 Mbps", "1.0 Mbps", "2.0 Mbps", "2.4 Mbps", "0.020", "202"],
    ]
    assert kpi(sec, "Fleet average throughput")["value"] == "14.0 Mbps"
    assert kpi(sec, "Fleet average throughput")["subtitle"] == "2 of 3 servers measured"
    assert kpi(sec, "Servers with interface errors")["value"] == "1"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "app-01", "value": 12_000_000.0}


def test_network_sort_by_errors(monkeypatch):
    ch = FakeCH([(("host_network_metrics_5m",), NET_ROWS)])
    (sec,), _, _ = run(monkeypatch, [{"id": "server_network_throughput",
                                      "options": {"sort": "errors"}}], ch)
    assert sec["title"] == "Top 10 Servers by Interface Errors"
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["db-01", "app-01"]
    assert sec["charts"][0]["series"]["points"] == [{"label": "db-01", "value": 0.02}]


# ── server_top_processes ────────────────────────────────────────────────────

PROC_ROWS = [
    # server, process, avg cpu, peak cpu, avg mem, peak mem, instances, samples, total groups
    (A, "java", 50.0, 80.0, 2_800_000_000.0, 3_000_000_000.0, 2, 168, 5),
    (B, "sqlservr", 20.0, 35.0, 6_000_000_000.0, 6_100_000_000.0, 1, 168, 5),
]


def test_top_processes_rows_and_clamped_window(monkeypatch):
    ch = FakeCH([(("host_process_metrics",), PROC_ROWS)])
    (sec,), ch, _ = run(monkeypatch, ["server_top_processes"], ch, frm=TO - timedelta(days=30))
    sql, params = ch.find("host_process_metrics")
    assert "pid > 0" in sql and "ORDER BY avg_cpu DESC" in sql
    assert params["f"] == "2026-09-23 12:00:00"  # clamped to the last 14 days
    assert params["n"] == 15
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 5 processes by average CPU"
    assert t["rows"][0] == ["java", "app-01", "50.0%", "80.0%", "2.6 GB", "2.8 GB", "2", "168"]
    assert kpi(sec, "Process groups observed")["value"] == "5"
    assert kpi(sec, "Highest average CPU")["subtitle"] == "java · app-01"
    assert any("covers the last 14 days of the period (2026-09-23 15:00 onwards)" in n
               for n in sec["notes"])


def test_top_processes_sort_by_memory(monkeypatch):
    ch = FakeCH([(("host_process_metrics",), PROC_ROWS[1:])])
    (sec,), ch, _ = run(monkeypatch, [{"id": "server_top_processes",
                                       "options": {"sort": "memory", "top_n": 1}}], ch)
    sql, params = ch.find("host_process_metrics")
    assert "ORDER BY peak_mem DESC" in sql and params["n"] == 1
    assert params["f"] == "2026-09-30 12:00:00"  # a 7-day window is not clamped
    assert sec["title"] == "Top 1 Processes by Memory"
    assert kpi(sec, "Largest peak memory")["value"] == "5.7 GB"
    assert not any("kept for 14 days, so" in n for n in sec["notes"])


# ── capacity_filesystem_forecast ────────────────────────────────────────────

def _fs_db(now):
    return FakeDB(filesystems=[
        (A, "/", 100e9, 57e9, 43e9, 57.0, now),
        (B, "C:", 100e9, 20e9, 80e9, 20.0, now),
        (C, "/data", 100e9, 96e9, 4e9, 96.0, now - timedelta(days=30)),  # stale
        (A, "/var", 100e9, 90e9, 10e9, 90.0, now),
        (B, "D:", 100e9, 30e9, None, None, now),
    ])


FS_FITS = [
    # server, mount, slope bytes/s, points, span seconds
    (A, "/", 1e9 / 86400, 2016, 604500),
    (B, "C:", 0.0, 2016, 604500),
    (A, "/var", 2e9 / 86400, 2016, 604500),
    (B, "D:", 5e9 / 86400, 5, 1200),  # too little history for a trend
]


def test_filesystem_forecast_days_to_full_and_status(monkeypatch):
    now = datetime.now(timezone.utc)
    ch = FakeCH([(("simpleLinearRegression",), FS_FITS)])
    db = _fs_db(now)
    (sec,), ch, db = run(monkeypatch, ["capacity_filesystem_forecast"], ch, db)
    inv_sql = next(q for q in db.calls if "server_filesystem_inventory" in q)
    assert "lower(f.fs_type) NOT IN" in inv_sql and "status <> 'disabled'" in inv_sql
    _, params = ch.find("simpleLinearRegression")
    assert params["epoch"] == int(FRM.replace(tzinfo=timezone.utc).timestamp())

    full_by = Branding(timezone="Asia/Riyadh").fmt_date(now + timedelta(days=5))
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["app-01", "/var", "90.0%", "93.1 GB", "9.3 GB", "+1.9 GB/day", "5",
                       full_by, "critical"]
    assert rows[1][:7] == ["app-01", "/", "57.0%", "93.1 GB", "40.0 GB", "+953.7 MB/day", "43"]
    assert rows[1][8] == "ok"
    assert rows[2] == ["db-01", "D:", "30.0%", "93.1 GB", "65.2 GB", "—", "—", "—", "ok"]
    assert rows[3] == ["db-01", "C:", "20.0%", "93.1 GB", "74.5 GB", "0 B/day", "—", "—", "ok"]
    assert len(rows) == 4  # the stale /data volume is left out
    assert kpi(sec, "Full within 30 days")["value"] == "1"
    assert kpi(sec, "Volumes ≥85% used")["value"] == "1"
    assert kpi(sec, "Fastest growth")["subtitle"] == "app-01 · /var"
    tracked = kpi(sec, "Volumes tracked")
    assert tracked["value"] == "4"
    assert tracked["subtitle"] == "1 without enough history for a trend"
    assert any("1 volume(s) not reported by their agent for more than 7 days" in n
               for n in sec["notes"])


def test_filesystem_forecast_horizon_option(monkeypatch):
    now = datetime.now(timezone.utc)
    ch = FakeCH([(("simpleLinearRegression",), FS_FITS)])
    (sec,), _, _ = run(monkeypatch, [{"id": "capacity_filesystem_forecast",
                                      "options": {"horizon": 60, "top_n": 2}}], ch, _fs_db(now))
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 4 volumes by time to full"
    assert [r[1] for r in t["rows"]] == ["/var", "/"]
    assert t["rows"][1][8] == "warning"  # 43 days is inside a 60-day horizon
    assert kpi(sec, "Full within 60 days")["value"] == "2"


def test_filesystem_forecast_without_inventory(monkeypatch):
    (sec,), ch, _ = run(monkeypatch, ["capacity_filesystem_forecast"])
    assert sec["notes"][0] == "No filesystem inventory for enabled servers."
    assert ch.calls == []  # nothing to fit


# ── every section: empty data ───────────────────────────────────────────────

@pytest.mark.parametrize("sid", PACK_IDS)
def test_every_section_has_an_empty_state(monkeypatch, sid):
    (sec,), _, _ = run(monkeypatch, [sid])
    assert sec["tables"] == []
    assert sec["notes"], sid
    (sec,), ch, _ = run(monkeypatch, [sid], db=FakeDB(servers=[]))
    assert sec["notes"] == [sp.NO_SERVERS] and ch.calls == []
