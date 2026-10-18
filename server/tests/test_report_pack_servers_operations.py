"""Server and agent operations section pack (report_packs/servers_operations).

Every section runs through report_sections.build_sections against fakes:
Postgres answers canned rows keyed by SQL substrings, ClickHouse likewise.
The window is the seven local days before today in Asia/Riyadh (UTC+3), so
day buckets, chart timestamps and table times all prove the organisation
timezone is honoured."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import servers_operations as so
from tests.conftest import _NullTransaction

RIYADH = ZoneInfo("Asia/Riyadh")
BRAND = Branding(timezone="Asia/Riyadh")
_TODAY = datetime.now(RIYADH).replace(hour=0, minute=0, second=0, microsecond=0)
TO = _TODAY.astimezone(timezone.utc).replace(tzinfo=None)       # local midnight, naive UTC
FRM = TO - timedelta(days=7)
DAYS = [int(FRM.replace(tzinfo=timezone.utc).timestamp()) + i * 86400 for i in range(7)]
# 12:00 UTC is 15:00 in Riyadh.
OCT1_NOON = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

SECTIONS = ("server_fleet_status", "server_telemetry_coverage", "server_uptime_reboots",
            "server_service_health", "server_event_log_errors", "server_alerts",
            "server_software_compliance", "server_software_inventory", "server_hardware_inventory",
            "agent_fleet_health", "agent_overhead")


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ── fakes ───────────────────────────────────────────────────────────────────

def _match(routes, sql, params):
    for key, rows in routes:
        keys = key if isinstance(key, tuple) else (key,)
        if all(k in sql for k in keys):
            return list(rows(sql, params) if callable(rows) else rows)
    return []


class Res:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql, p = str(statement), dict(params or {})
        self.calls.append((sql, p))
        return Res(_match(self.routes, sql, p))

    def begin_nested(self):
        return _NullTransaction()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def call(self, needle: str) -> tuple[str, dict]:
        return next(c for c in self.calls if needle in c[0])


class _CHResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        p = dict(parameters or {})
        self.calls.append((sql, p))
        return _CHResult(_match(self.routes, sql, p))

    def call(self, *needles: str) -> tuple[str, dict]:
        return next(c for c in self.calls if all(n in c[0] for n in needles))


def run(monkeypatch, sid, db, ch=None, title=None, frm=FRM, to=TO, **options):
    ch = ch or FakeCH()
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    out = asyncio.run(rs.build_sections(db, [{"id": sid, "title": title, "options": options}], frm, to,
                                        None, requester=None, branding=BRAND))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["title"], row)
            assert all(isinstance(c, str) for c in row), row
    for k in sec["kpis"]:
        assert isinstance(k["value"], str), k
    return sec


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


def table(sec, prefix):
    return next(t for t in sec["tables"] if t["title"].startswith(prefix))


def chart(sec, title):
    return next(c for c in sec["charts"] if c["title"].startswith(title))


# ── registry and templates ──────────────────────────────────────────────────

def test_sections_are_registered_fleet_scoped_with_server_permissions():
    for sid in SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["fn"].__module__ == so.__name__
        assert e["scope"] == "fleet", sid
        assert "servers.view" in e["requires"], sid
        assert e["category"] != "Traffic"
    assert rs.SECTION_REGISTRY["server_alerts"]["requires"] == ("servers.view", "alerts.view")
    assert not rs.may_view_section(frozenset({"servers.view"}), "server_alerts")
    assert rs.may_view_section(frozenset({"servers.view", "alerts.view"}), "server_alerts")
    snapshots = {sid for sid in SECTIONS if rs.SECTION_REGISTRY[sid]["semantics"] == "snapshot"}
    assert snapshots == {"server_fleet_status", "server_uptime_reboots", "server_software_compliance",
                         "server_software_inventory", "server_hardware_inventory", "agent_fleet_health"}


def test_templates_are_valid_and_published():
    keys = {"servers_fleet_health", "servers_telemetry_coverage", "servers_reboots_uptime",
            "servers_windows_services", "servers_event_log_errors", "servers_alerts",
            "servers_software_compliance", "servers_software_inventory", "servers_hardware_inventory",
            "agents_fleet_health", "agents_resource_overhead"}
    assert keys <= set(rt.TEMPLATES)
    assert rt.validate_catalog() == []
    assert rt.TEMPLATES["servers_fleet_health"].featured
    for key in keys:
        t = rt.TEMPLATES[key]
        assert t.category in ("servers", "inventory", "security"), key
        assert set(t.audience) <= set(rt.AUDIENCES)
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
        assert t.requires() >= {"servers.view"}
    # A viewer without alerts.view still gets the fleet template, minus that section.
    perms = frozenset({"servers.view"})
    assert rt.is_available(rt.TEMPLATES["servers_fleet_health"], perms)
    assert "server_alerts" not in rt.available_sections(rt.TEMPLATES["servers_fleet_health"], perms)


# ── server_fleet_status ─────────────────────────────────────────────────────

def _fleet_db():
    return FakeDB([
        ("AS status, COUNT(*) AS n FROM servers s GROUP BY 1",
         [{"status": s, "n": n} for s, n in (("healthy", 7), ("warning", 2), ("critical", 1),
                                              ("stale", 1), ("unknown", 1), ("disabled", 3))]),
        ("AS os, COUNT(*)", [{"os": "windows", "n": 9}, {"os": "linux", "n": 3}]),
        ("s.status_reasons", lambda sql, p: [
            {"server": "db-01", "os_type": "windows", "os_name": "Windows Server 2022", "site": "Riyadh DC",
             "status": "critical", "status_reasons": json.dumps(["Filesystem C: at 97.1% (critical ≥ 95%)",
                                                                  "CPU at 99% (critical ≥ 98%)"]),
             "last_seen": OCT1_NOON},
            {"server": "app-02", "os_type": "linux", "os_name": None, "site": None, "status": "stale",
             "status_reasons": [], "last_seen": None},
        ][: p["lim"]]),
    ])


def test_fleet_status_kpis_charts_and_attention_list(monkeypatch):
    db = _fleet_db()
    sec = run(monkeypatch, "server_fleet_status", db)
    assert kpi(sec, "Monitored servers")["value"] == "12"
    assert kpi(sec, "Monitored servers")["subtitle"] == "3 disabled"
    assert kpi(sec, "Healthy")["value"] == "7"
    assert kpi(sec, "Healthy")["subtitle"] == "58.3% of 12"
    assert kpi(sec, "Critical")["accent"] == "danger"
    assert kpi(sec, "Not reporting")["value"] == "2"
    donut = chart(sec, "Servers by status")["series"]
    assert donut["kind"] == "donut"
    assert [p["label"] for p in donut["points"]][:3] == ["Critical", "Warning", "Stale (not reporting)"]
    assert {p["label"] for p in chart(sec, "Servers by operating system")["series"]["points"]} == {"Windows",
                                                                                                    "Linux"}
    t = table(sec, "Servers needing attention")
    assert t["title"] == "Servers needing attention (top 2 of 5)"
    assert t["rows"][0] == ["db-01", "Windows Server 2022", "Riyadh DC", "critical",
                            "Filesystem C: at 97.1% (critical ≥ 95%); CPU at 99% (critical ≥ 98%)",
                            "Oct 01 15:00"]
    # Stale = not measured: explained, not a failure reason invented from data.
    assert t["rows"][1][3:] == ["stale", "No data received from the agent", "—"]
    assert t["rows"][1][1] == "Linux"
    assert any("not measured" in n for n in sec["notes"])


def test_fleet_status_top_n_and_os_name_option(monkeypatch):
    db = _fleet_db()
    sec = run(monkeypatch, "server_fleet_status", db, top_n=1, os_group="name")
    assert db.call("s.status_reasons")[1]["lim"] == 1
    assert table(sec, "Servers needing attention")["title"] == "Servers needing attention (top 1 of 5)"
    assert "s.os_name" in db.call("AS os, COUNT(*)")[0]
    assert chart(sec, "Servers by operating system")["title"].endswith("(name)")


def test_fleet_status_empty(monkeypatch):
    sec = run(monkeypatch, "server_fleet_status", FakeDB())
    assert sec["notes"] == ["No servers are registered."]
    assert not sec["tables"]


# ── server_telemetry_coverage ───────────────────────────────────────────────

LONG_AGO = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _coverage_fixtures():
    servers = [
        {"id": "a", "server": "full-01", "status": "healthy", "collection_mode": "agent", "created_at": LONG_AGO,
         "metric_interval_s": 30, "agents": 1, "first_enrolled": LONG_AGO, "last_metric_at": OCT1_NOON},
        {"id": "b", "server": "half-02", "status": "warning", "collection_mode": "agent", "created_at": LONG_AGO,
         "metric_interval_s": 60, "agents": 1, "first_enrolled": LONG_AGO, "last_metric_at": OCT1_NOON},
        {"id": "c", "server": "silent-03", "status": "stale", "collection_mode": "agent", "created_at": LONG_AGO,
         "metric_interval_s": None, "agents": 1, "first_enrolled": LONG_AGO, "last_metric_at": None},
        {"id": "d", "server": "noagent-04", "status": "unknown", "collection_mode": "agent",
         "created_at": LONG_AGO, "metric_interval_s": None, "agents": 0, "first_enrolled": None,
         "last_metric_at": None},
        {"id": "e", "server": "snmp-05", "status": "healthy", "collection_mode": "snmp", "created_at": LONG_AGO,
         "metric_interval_s": None, "agents": 0, "first_enrolled": None, "last_metric_at": None},
        {"id": "f", "server": "new-06", "status": "healthy", "collection_mode": "agent", "created_at": LONG_AGO,
         "metric_interval_s": 30, "agents": 1, "first_enrolled": TO.replace(tzinfo=timezone.utc) + timedelta(hours=1),
         "last_metric_at": None},
    ]
    # 7 days at 30 s = 20,160 samples; at 60 s = 10,080. Over-delivery is capped.
    ch = FakeCH([("host_cpu_metrics_5m", [("a", 25000), ("b", 5040)])])
    return FakeDB([("LEFT JOIN agent_policies pol", servers)]), ch


def test_telemetry_coverage_received_vs_expected(monkeypatch):
    db, ch = _coverage_fixtures()
    sec = run(monkeypatch, "server_telemetry_coverage", db, ch)
    sql, params = ch.call("host_cpu_metrics_5m")
    assert "sum(sample_count)" in sql and params == {"f": FRM, "t": TO}
    # (20,160 capped + 5,040) / (20,160 + 10,080 + 20,160)
    assert kpi(sec, "Telemetry samples received")["value"] == "50.00%"
    assert kpi(sec, "Telemetry samples received")["subtitle"] == \
        "25,200 of 50,400 expected · 3 agent server(s) · over the period"
    assert [(f["severity"], f["text"]) for f in sec["findings"]] == [
        ("danger", "1 agent-monitored server(s) sent no metrics in the period"),
        ("warning", "1 server(s) delivered less than 95% of their expected samples"),
        ("warning", "1 agent-mode server(s) have no enrolled agent and are not monitored"),
    ]
    assert not any("Coverage compares" in f["text"] for f in sec["findings"])
    assert kpi(sec, "Servers ≥ 95%")["value"] == "1"
    assert kpi(sec, "Servers with gaps")["value"] == "1"
    assert kpi(sec, "Silent servers")["value"] == "1"
    assert kpi(sec, "No agent")["subtitle"] == "Not measured"
    t = table(sec, "Servers below 95% coverage")
    assert [r[0] for r in t["rows"]] == ["silent-03", "half-02"]
    assert t["rows"][0][1:5] == ["30 s", "0", "20,160", "0.00%"]
    assert t["rows"][1][1:6] == ["60 s", "5,040", "10,080", "50.00%", "Oct 01 15:00"]
    assert t["rows"][0][5] == "—"
    notes = " ".join(sec["notes"])
    assert "another collection mode" in notes and "no enrolled agent" in notes
    assert "enrolled after the period" in notes
    bands = {p["label"]: p["value"] for p in chart(sec, "Servers by telemetry coverage")["series"]["points"]}
    assert bands == {"Meets target": 1, "Gaps": 1, "Silent": 1, "No agent": 1}


def test_telemetry_coverage_threshold_and_top_n(monkeypatch):
    db, ch = _coverage_fixtures()
    sec = run(monkeypatch, "server_telemetry_coverage", db, ch, threshold=40)
    assert kpi(sec, "Servers ≥ 40%")["value"] == "2"
    assert [r[0] for r in table(sec, "Servers below 40% coverage")["rows"]] == ["silent-03"]
    db, ch = _coverage_fixtures()
    sec = run(monkeypatch, "server_telemetry_coverage", db, ch, top_n=1)
    assert table(sec, "Servers below")["title"] == "Servers below 95% coverage (top 1 of 2)"


def test_telemetry_coverage_empty(monkeypatch):
    sec = run(monkeypatch, "server_telemetry_coverage", FakeDB())
    assert sec["notes"] == ["No servers are registered."]


# ── server_uptime_reboots ───────────────────────────────────────────────────

def _uptime_db():
    now = datetime.now(timezone.utc)
    return FakeDB([
        ("AS long_running", [{"servers": 10, "no_boot": 2, "rebooted": 3, "reporting": 7, "up_1d": 1,
                              "up_7d": 2, "up_30d": 0, "up_90d": 1, "up_more": 3, "long_running": 3}]),
        ("ORDER BY s.boot_time DESC", lambda sql, p: [
            {"server": "web-01", "os_type": "windows", "os_name": "Windows Server 2019", "status": "healthy",
             "boot_time": OCT1_NOON},
            {"server": "web-02", "os_type": "linux", "os_name": "", "status": "stale", "boot_time": OCT1_NOON},
        ][: p["lim"]]),
        ("ORDER BY s.boot_time ASC", [
            {"server": "legacy-01", "os_type": "windows", "os_name": "Windows Server 2012 R2",
             "status": "healthy", "boot_time": now - timedelta(days=400)}]),
    ])


def test_uptime_reboots_kpis_tables_and_disclosure(monkeypatch):
    db = _uptime_db()
    sec = run(monkeypatch, "server_uptime_reboots", db)
    assert db.call("AS long_running")[1] == {"f": FRM, "t": TO, "long": 90}
    assert kpi(sec, "Rebooted in period")["value"] == "3"
    assert kpi(sec, "Longest uptime")["value"].startswith("400d")
    assert kpi(sec, "Longest uptime")["subtitle"] == "legacy-01"
    assert kpi(sec, "Up 90+ days")["value"] == "3"
    assert kpi(sec, "Boot time not reported")["subtitle"] == "Not measured"
    t = table(sec, "Servers rebooted in this period")
    assert t["title"] == "Servers rebooted in this period (top 2 of 3)"
    assert t["rows"][0][:4] == ["web-01", "Windows Server 2019", "healthy", "2026-10-01 15:00"]
    assert re.match(r"^\d+d \d+h$", t["rows"][0][4])
    # A stale server's current uptime is unknown, never computed from an old boot.
    assert t["rows"][1][2:] == ["stale", "2026-10-01 15:00", "—"]
    assert any("most recent boot" in n for n in sec["notes"])
    bands = chart(sec, "Current uptime")["series"]["points"]
    assert {p["label"]: p["value"] for p in bands} == {"Under 1 day": 1, "1–7 days": 2, "30–90 days": 1,
                                                       "90 days or more": 3}


def test_uptime_long_threshold_option(monkeypatch):
    db = _uptime_db()
    sec = run(monkeypatch, "server_uptime_reboots", db, long_uptime_days=30, top_n=1)
    assert db.call("AS long_running")[1]["long"] == 30
    assert kpi(sec, "Up 30+ days")["subtitle"] == "No reboot in 30 days or more"
    assert db.call("ORDER BY s.boot_time DESC")[1]["lim"] == 1


# ── server_service_health ───────────────────────────────────────────────────

def _service_fixtures():
    db = FakeDB([
        ("AS inv_servers", [{"inv_servers": 10, "down": 3, "servers": 2, "watched": 1, "stale": 4}]),
        ("ORDER BY watched DESC", lambda sql, p: [
            {"server": "iis-01", "service_name": "W3SVC", "display_name": "World Wide Web Publishing Service",
             "start_mode": "auto", "state": "stopped", "watched": True, "updated_at": OCT1_NOON},
            {"server": "iis-01", "service_name": "gupdate", "display_name": "Google Update Service",
             "start_mode": "auto", "state": "stopped", "watched": False, "updated_at": OCT1_NOON},
        ][: p["lim"]]),
        ("GROUP BY service_name ORDER BY servers DESC", [
            {"service_name": "gupdate", "display_name": "Google Update Service", "servers": 2}]),
        ("WHERE s.id::text = ANY(:ids)", [{"id": "s1", "name": "iis-01"}, {"id": "s2", "name": "sql-01"}]),
    ])
    ch = FakeCH([
        ("HAVING changes > 0", [("s1", "W3SVC", 5, 2, "stopped", OCT1_NOON),
                                ("s2", "MSSQLSERVER", 3, 1, "running", OCT1_NOON)]),
        (("toUnixTimestamp(", "host_service_state"), [(DAYS[2], 2), (DAYS[5], 1)]),
    ])
    return db, ch


def test_service_health_snapshot_changes_and_daily_stops(monkeypatch):
    db, ch = _service_fixtures()
    sec = run(monkeypatch, "server_service_health", db, ch)
    assert kpi(sec, "Services not running")["value"] == "3"
    assert kpi(sec, "Services not running")["subtitle"] == "Auto-start or watched · on 2 of 10 servers"
    assert kpi(sec, "Watched services not running")["accent"] == "danger"
    assert kpi(sec, "State changes in period")["value"] == "8"
    assert kpi(sec, "State changes in period")["subtitle"] == "3 stops"
    down = table(sec, "Automatic-start or watched services not running")
    assert down["rows"][0] == ["iis-01", "W3SVC", "World Wide Web Publishing Service", "auto", "stopped",
                               "Yes", "Oct 01 15:00"]
    changes = table(sec, "Services that changed state most often")
    assert changes["rows"][0] == ["iis-01", "W3SVC", "5", "2", "stopped", "Oct 01 15:00"]
    assert changes["rows"][1][0] == "sql-01"
    stops = chart(sec, "Service stops per day")["series"]
    assert [p["t"] for p in stops["points"]] == [_iso(d) for d in DAYS]
    assert stops["points"][0]["t"].endswith("T21:00:00Z")          # local midnight in Riyadh
    assert [p["v"] for p in stops["points"]] == [0, 0, 2, 0, 0, 1, 0]
    sql, params = ch.call("toUnixTimestamp(", "host_service_state")
    assert "INTERVAL 1 DAY, %(org_tz)s" in sql and params["org_tz"] == "Asia/Riyadh"
    assert "lagInFrame" in ch.call("HAVING changes > 0")[0]
    assert any("4 stopped service record(s)" in n for n in sec["notes"])
    assert chart(sec, "Most common stopped")["series"]["points"] == [{"label": "Google Update Service",
                                                                      "value": 2}]


def test_service_health_top_n(monkeypatch):
    db, ch = _service_fixtures()
    sec = run(monkeypatch, "server_service_health", db, ch, top_n=1)
    assert db.call("ORDER BY watched DESC")[1]["lim"] == 1
    assert table(sec, "Automatic-start")["title"].endswith("(top 1 of 3)")
    assert table(sec, "Services that changed")["title"].endswith("(top 1 of 2)")
    assert len(table(sec, "Services that changed")["rows"]) == 1


def test_service_health_empty(monkeypatch):
    sec = run(monkeypatch, "server_service_health", FakeDB())
    assert sec["notes"][0].startswith("No Windows service inventory")


# ── server_event_log_errors ─────────────────────────────────────────────────

def _event_ch():
    return FakeCH([
        ("AS servers_reporting", [(3, 40, 100, 4, 9, 6, 500)]),
        ("AS n, count() AS summaries", lambda sql, p: [(20, 300)]),
        (("toUnixTimestamp(", "host_event_log_summary"), [(DAYS[0], 3, 10, 5), (DAYS[2], 0, 30, 95)]),
        ("GROUP BY server_id, log_name", lambda sql, p: [
            ("s1", "System", 3, 25, 0, 28, OCT1_NOON), ("s2", "Application", 0, 15, 0, 15, OCT1_NOON),
        ][: p["lim"]]),
    ])


def _event_db():
    return FakeDB([("WHERE s.id::text = ANY(:ids)", [{"id": "s1", "name": "dc-01"}, {"id": "s2", "name": "app-01"}])])


def test_event_log_errors_daily_gaps_and_top_logs(monkeypatch):
    ch = _event_ch()
    sec = run(monkeypatch, "server_event_log_errors", _event_db(), ch)
    assert sec["title"] == "Event Log Errors"
    assert kpi(sec, "Critical events")["value"] == "3"
    assert kpi(sec, "Error events")["value"] == "40"
    assert kpi(sec, "Total")["value"] == "43"
    assert kpi(sec, "Total")["subtitle"] == "▲ 115.0% vs previous period"
    assert kpi(sec, "Servers affected")["subtitle"] == "of 9 sending event log summaries"
    assert all(k["label"] != "Warning events" for k in sec["kpis"])
    # The previous window is queried with the previous bounds.
    pf, pt = FRM - timedelta(days=7), FRM
    assert ch.call("AS n, count() AS summaries")[1] == {"f": pf, "t": pt, "levels": ("critical", "error")}
    assert ch.call("AS servers_reporting")[1]["levels"] == ("critical", "error")
    series = chart(sec, "Events per day")["series"]
    assert series["kind"] == "stacked"
    crit, err = series["series"]
    assert crit["name"] == "Critical" and err["name"] == "Error"
    assert [p["t"] for p in err["points"]] == [_iso(d) for d in DAYS]
    # Days without any summary are gaps (null), days with summaries count from 0.
    assert [p["v"] for p in err["points"]] == [10, None, 30, None, None, None, None]
    assert [p["v"] for p in crit["points"]][:3] == [3, None, 0]
    t = table(sec, "Server logs with the most events")
    assert t["headers"] == ["Server", "Log", "Critical", "Error", "Total", "Last event"]
    assert t["rows"][0] == ["dc-01", "System", "3", "25", "28", "Oct 01 15:00"]
    assert t["title"] == "Server logs with the most events (top 2 of 6)"


def test_event_log_level_option_includes_warnings(monkeypatch):
    ch = _event_ch()
    sec = run(monkeypatch, "server_event_log_errors", _event_db(), ch, level="warning", top_n=1)
    assert sec["title"] == "Event Log Errors and Warnings"
    assert ch.call("AS servers_reporting")[1]["levels"] == ("critical", "error", "warning")
    assert kpi(sec, "Warning events")["value"] == "100"
    assert kpi(sec, "Total")["value"] == "143"
    assert len(chart(sec, "Events per day")["series"]["series"]) == 3
    t = table(sec, "Server logs")
    assert t["headers"] == ["Server", "Log", "Critical", "Error", "Warning", "Total", "Last event"]
    assert len(t["rows"]) == 1 and ch.call("GROUP BY server_id, log_name")[1]["lim"] == 1


def test_event_log_without_summaries(monkeypatch):
    sec = run(monkeypatch, "server_event_log_errors", FakeDB(), FakeCH())
    assert sec["notes"][0] == "No event log summaries were received in this period."
    assert not sec["kpis"]


# ── server_alerts ───────────────────────────────────────────────────────────

def _alerts_db():
    return FakeDB([
        ("COUNT(DISTINCT a.server_id) AS servers", [{"total": 12, "critical": 3, "warning": 7, "info": 2,
                                                     "open": 2, "resolved": 9, "servers": 4, "mttr_s": 5400}]),
        ("SELECT COUNT(*) AS total FROM alerts a WHERE", [{"total": 8}]),
        ("date_trunc('day'", [{"d": DAYS[1], "critical": 1, "warning": 2, "info": 0},
                              {"d": DAYS[4], "critical": 2, "warning": 5, "info": 2}]),
        ("AS source", [{"source": "host_metric", "n": 6}, {"source": "agent_offline", "n": 4},
                       {"source": "custom_probe", "n": 2}]),
        ("JOIN servers s ON s.id = a.server_id", lambda sql, p: [
            {"server": "db-01", "total": 6, "critical": 2, "warning": 4, "open": 1, "mttr_s": 600,
             "last_alert": OCT1_NOON},
            {"server": "web-01", "total": 4, "critical": 1, "warning": 2, "open": 1, "mttr_s": None,
             "last_alert": OCT1_NOON},
        ][: p["lim"]]),
    ])


def test_server_alerts_kpis_trend_sources_and_top_servers(monkeypatch):
    db = _alerts_db()
    sec = run(monkeypatch, "server_alerts", db)
    assert kpi(sec, "Server alerts")["value"] == "12"
    assert kpi(sec, "Server alerts")["subtitle"] == "▲ 50.0% vs previous period"
    assert kpi(sec, "Critical")["subtitle"] == "25.0% of 12"
    assert kpi(sec, "Mean time to resolve")["value"] == "1h 30m"
    assert kpi(sec, "Still open")["value"] == "2"
    sql, params = db.call("COUNT(DISTINCT a.server_id) AS servers")
    assert "a.status <> 'pending'" in sql and "hold_cancelled" in sql and "is_recovery" in sql
    assert db.call("date_trunc('day'")[1]["tz"] == "Asia/Riyadh"
    assert db.call("SELECT COUNT(*) AS total FROM alerts a WHERE")[1] == {"f": FRM - timedelta(days=7), "t": FRM}
    per_day = chart(sec, "Server alerts per day")["series"]
    assert [s["name"] for s in per_day["series"]] == ["Critical", "Warning", "Info"]
    assert [p["v"] for p in per_day["series"][1]["points"]] == [0, 2, 0, 0, 5, 0, 0]
    assert per_day["series"][1]["points"][1]["t"] == _iso(DAYS[1])
    labels = [p["label"] for p in chart(sec, "Alerts by source")["series"]["points"]]
    assert labels == ["Alert rules", "Agent offline", "Custom probe"]
    t = table(sec, "Servers with the most alerts")
    assert t["rows"][0] == ["db-01", "6", "2", "4", "1", "10m", "Oct 01 15:00"]
    assert t["rows"][1][5] == "—"                     # nothing resolved: not measured
    assert t["title"] == "Servers with the most alerts (top 2 of 4)"


def test_server_alerts_top_n_and_empty(monkeypatch):
    db = _alerts_db()
    run(monkeypatch, "server_alerts", db, top_n=1)
    assert db.call("JOIN servers s ON s.id = a.server_id")[1]["lim"] == 1
    sec = run(monkeypatch, "server_alerts", FakeDB([("SELECT COUNT(*) AS total FROM alerts a WHERE",
                                                     [{"total": 3}])]))
    assert sec["notes"] == ["No server alerts were raised in this period."]
    assert kpi(sec, "Server alerts")["subtitle"] == "▼ 100.0% vs previous period"


# ── server_software_compliance ──────────────────────────────────────────────

def _scope(b_os="windows", b_tags=(), s_os="windows", s_tags=(), s_status="healthy", enabled=True):
    return {"_b_enabled": enabled, "_b_os_type": b_os, "_b_site_id": None, "_b_match_tags": json.dumps(list(b_tags)),
            "_s_os_type": s_os, "_s_site_id": None, "_s_tags": list(s_tags), "_s_status": s_status}


def _pair(baseline, server, results, violations=0, critical=0, warning=0, missing=0, outdated=0, prohibited=0,
          info=0, **scope):
    return {"baseline_id": baseline, "baseline": baseline.upper(), "server_id": server, "server": server,
            "results": results, "violations": violations, "critical": critical, "warning": warning,
            "info": info, "missing": missing, "outdated": outdated, "prohibited": prohibited,
            "evaluated_at": OCT1_NOON, **_scope(**scope)}


def _compliance_db():
    pairs = [
        _pair("b1", "srv-1", 5),
        _pair("b1", "srv-2", 5, violations=2, critical=1, warning=1, missing=1, outdated=1),
        # Stale row: a Windows baseline result kept for a server that is now Linux.
        _pair("b1", "srv-3", 5, violations=3, critical=3, missing=3, s_os="linux"),
        _pair("b2", "srv-4", 2, b_os=None, b_tags=["prod"], s_tags=["prod", "web"]),
    ]
    failing_since = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)   # 1 Oct in Riyadh
    detail = [
        {"server": "srv-2", "baseline": "B1", "rule_type": "required", "package_match": "CrowdStrike",
         "min_version": None, "status": "missing", "severity": "critical", "found_package": None,
         "found_version": None, "expected": "CrowdStrike", "first_failed_at": failing_since, **_scope()},
        {"server": "srv-2", "baseline": "B1", "rule_type": "required", "package_match": "7-Zip",
         "min_version": "23.01", "status": "outdated", "severity": "warning", "found_package": "7-Zip 19.00",
         "found_version": "19.00", "expected": "7-Zip ≥ 23.01", "first_failed_at": None, **_scope()},
        {"server": "srv-3", "baseline": "B1", "rule_type": "required", "package_match": "CrowdStrike",
         "min_version": None, "status": "missing", "severity": "critical", "found_package": None,
         "found_version": None, "expected": "CrowdStrike", "first_failed_at": failing_since,
         **_scope(s_os="linux")},
    ]
    return FakeDB([("GROUP BY b.id, s.id", pairs),
                   ("JOIN software_baseline_rules br", lambda sql, p: [r for r in detail if r["severity"] in p["sevs"]])])


def test_software_compliance_drops_out_of_scope_rows(monkeypatch):
    db = _compliance_db()
    sec = run(monkeypatch, "server_software_compliance", db)
    assert kpi(sec, "Servers compliant")["value"] == "66.7%"
    assert kpi(sec, "Servers compliant")["subtitle"] == "2 of 3 servers"
    assert kpi(sec, "Violations")["value"] == "2"
    assert kpi(sec, "Critical violations")["value"] == "1"
    assert kpi(sec, "Rule checks passing")["value"] == "83.3%"
    by_baseline = table(sec, "Compliance by baseline")
    assert by_baseline["rows"] == [["B1", "2", "1", "50.0%", "2", "1"], ["B2", "1", "1", "100.0%", "0", "0"]]
    v = table(sec, "Open violations")
    assert [r[0] for r in v["rows"]] == ["srv-2", "srv-2"]               # srv-3 is out of scope
    assert v["rows"][0] == ["srv-2", "B1", "Require CrowdStrike", "missing", "critical", "—", "CrowdStrike",
                            "2026-10-01"]
    assert v["rows"][1][5] == "7-Zip 19.00 19.00" and v["rows"][1][7] == "—"
    sev = {p["label"]: p["value"] for p in chart(sec, "Violations by severity")["series"]["points"]}
    assert sev == {"Critical": 1, "Warning": 1}
    assert "b.enabled AND s.status <> 'disabled'" in db.call("GROUP BY b.id, s.id")[0]


def test_software_compliance_severity_option(monkeypatch):
    db = _compliance_db()
    sec = run(monkeypatch, "server_software_compliance", db, min_severity="critical", top_n=5)
    assert db.call("JOIN software_baseline_rules br")[1]["sevs"] == ["critical"]
    v = table(sec, "Open violations")
    assert v["title"] == "Open violations (critical only)"
    assert [r[4] for r in v["rows"]] == ["critical"]


def test_software_compliance_without_baselines(monkeypatch):
    sec = run(monkeypatch, "server_software_compliance", FakeDB())
    assert sec["notes"][0].startswith("No software baseline results")


# ── server_software_inventory ───────────────────────────────────────────────

def _software_db():
    return FakeDB([
        ("AS sprawl", [{"packages": 120, "installs": 900, "servers": 9, "new_installs": 3, "new_servers": 2,
                        "fleet": 10, "sprawl": 2}]),
        ("MAX(i.vendor) AS vendor", lambda sql, p: [
            {"package": "7-Zip", "vendor": "Igor Pavlov", "servers": 9, "versions": 2},
            {"package": "ZenPlus Agent", "vendor": None, "servers": 5, "versions": 2},
        ][: p["lim"]]),
        ("ORDER BY versions DESC", [{"package": "7-Zip", "versions": 2, "servers": 9},
                                    {"package": "ZenPlus Agent", "versions": 2, "servers": 5}]),
        ("ANY(:names)", [{"package": "7-Zip", "version": "19.00", "servers": 3},
                         {"package": "7-Zip", "version": "23.01", "servers": 6},
                         {"package": "ZenPlus Agent", "version": "1.9.0", "servers": 4},
                         {"package": "ZenPlus Agent", "version": "1.10.0", "servers": 1}]),
        ("ORDER BY i.install_date DESC", [
            {"server": "web-01", "package": "Notepad++", "version": "8.6", "vendor": "Notepad++ Team",
             "install_date": datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc)}]),
    ])


def test_software_inventory_top_sprawl_and_installed(monkeypatch):
    db = _software_db()
    sec = run(monkeypatch, "server_software_inventory", db)
    assert kpi(sec, "Distinct packages")["value"] == "120"
    assert kpi(sec, "Servers inventoried")["subtitle"] == "90.0% of 10"
    assert kpi(sec, "Packages with several versions")["value"] == "2"
    top = table(sec, "Most widely installed software")
    assert top["title"] == "Most widely installed software (top 2 of 120)"
    assert top["rows"][1] == ["ZenPlus Agent", "—", "5", "2"]
    sprawl = table(sec, "Version sprawl")
    # Versions compare numerically: 1.10.0 is newer than 1.9.0.
    assert sprawl["rows"] == [["7-Zip", "2", "9", "23.01", "3"], ["ZenPlus Agent", "2", "5", "1.10.0", "4"]]
    installed = table(sec, "Installed in this period")
    assert installed["rows"][0] == ["web-01", "Notepad++", "8.6", "Notepad++ Team", "2026-10-01"]
    assert installed["title"] == "Installed in this period (top 1 of 3)"
    assert any("not collected" in n for n in sec["notes"])
    assert db.call("ORDER BY i.install_date DESC")[1]["f"] == FRM


def test_software_inventory_top_n_and_empty(monkeypatch):
    db = _software_db()
    sec = run(monkeypatch, "server_software_inventory", db, top_n=1)
    assert db.call("MAX(i.vendor) AS vendor")[1]["lim"] == 1
    assert len(table(sec, "Most widely")["rows"]) == 1
    sec = run(monkeypatch, "server_software_inventory", FakeDB())
    assert sec["notes"][0].startswith("No software inventory")


# ── server_hardware_inventory ───────────────────────────────────────────────

GIB = 1024 ** 3


def _hardware_db():
    return FakeDB([
        ("AS mnone", [{"servers": 3, "with_hw": 2, "logical": 24, "physical": 12, "memory": 48 * GIB,
                       "disks": 3, "disk_bytes": 2 * 1024 * GIB, "faulty": 1, "m8": 0, "m16": 1, "m32": 1,
                       "m64": 0, "m128": 0, "mmore": 0, "mnone": 1}]),
        ("s.cpu_physical_cores, s.cpu_cores", [
            {"server": "db-01", "os_type": "windows", "os_name": "Windows Server 2022", "os_version": "10.0",
             "architecture": "amd64", "cpu_model": "Intel Xeon Gold 6338", "cpu_physical_cores": 8,
             "cpu_cores": 16, "memory_total_bytes": 32 * GIB, "disks": 2, "disk_bytes": 1024 * GIB, "faulty": 1},
            {"server": "old-02", "os_type": "linux", "os_name": None, "os_version": None, "architecture": None,
             "cpu_model": None, "cpu_physical_cores": None, "cpu_cores": None, "memory_total_bytes": None,
             "disks": 0, "disk_bytes": None, "faulty": 0},
        ]),
        ("AS model", [{"model": "Intel Xeon Gold 6338", "n": 2}, {"model": "Not reported", "n": 1}]),
    ])


def test_hardware_inventory_rows_kpis_and_missing_values(monkeypatch):
    db = _hardware_db()
    sec = run(monkeypatch, "server_hardware_inventory", db)
    assert kpi(sec, "Servers with hardware data")["subtitle"] == "66.7% of 3"
    assert kpi(sec, "Logical CPU cores")["value"] == "24"
    assert kpi(sec, "Logical CPU cores")["subtitle"] == "12 physical"
    assert kpi(sec, "Installed memory")["value"] == "48.0 GB"
    assert kpi(sec, "Physical disks")["subtitle"] == "2.0 TB raw capacity"
    assert kpi(sec, "Disks reporting a fault")["accent"] == "danger"
    t = table(sec, "Server hardware")
    assert t["rows"][0] == ["db-01", "Windows Server 2022 (amd64)", "Intel Xeon Gold 6338", "8 / 16",
                            "32.0 GB", "2 · 1.0 TB", "degraded"]
    assert t["rows"][1] == ["old-02", "Linux", "—", "—", "—", "—", "—"]
    assert "ORDER BY server" in db.call("s.cpu_physical_cores, s.cpu_cores")[0]
    bands = {p["label"]: p["value"] for p in chart(sec, "Servers by installed memory")["series"]["points"]}
    assert bands == {"9–16 GB": 1, "17–32 GB": 1, "Not reported": 1}


def test_hardware_inventory_sort_option(monkeypatch):
    db = _hardware_db()
    sec = run(monkeypatch, "server_hardware_inventory", db, sort="memory", top_n=1)
    sql, params = db.call("s.cpu_physical_cores, s.cpu_cores")
    assert "ORDER BY s.memory_total_bytes DESC NULLS LAST, server" in sql and params["lim"] == 1
    assert table(sec, "Server hardware")["title"].startswith("Server hardware by memory")


# ── agent_fleet_health ──────────────────────────────────────────────────────

def _agents_db(cert_reported=8):
    expired = datetime.now(timezone.utc) - timedelta(days=2)
    return FakeDB([
        ("AS attention", [{"agents": 10, "revoked": 1, "pending_auth": 1, "cert_reported": cert_reported,
                           "cert_expiring": 2, "cert_expired": 1, "config_errors": 1, "update_failed": 1,
                           "updating": 2, "attention": 4}]),
        ("SELECT a.status, COUNT(*) AS n FROM agents a", [{"status": "online", "n": 6}, {"status": "stale", "n": 1},
                                                          {"status": "offline", "n": 2},
                                                          {"status": "enrolling", "n": 1}]),
        ("AS version, COUNT(*) AS n", [{"platform": "windows", "version": "1.14.2", "n": 5},
                                       {"platform": "windows", "version": "1.13.0", "n": 3},
                                       {"platform": "linux", "version": "1.10.0", "n": 1},
                                       {"platform": "linux", "version": "", "n": 1}]),
        ("FROM agent_packages p", [{"platform": "windows", "version": "1.14.2"},
                                   {"platform": "windows", "version": "1.9.9"},
                                   {"platform": "linux", "version": "1.9.0"}]),
        ("AS upd_state", lambda sql, p: [
            {"agent": "web-01", "server": "web-01.corp", "platform": "windows", "status": "offline",
             "version": "1.13.0", "last_heartbeat_at": OCT1_NOON, "pending": False,
             "certificate_expires_at": expired, "config_apply_error": "invalid event log filter",
             "upd_state": "failed", "upd_error": "signature mismatch", "upd_target": "1.14.2", "conflict": False},
            {"agent": "new-host", "server": None, "platform": "windows", "status": "enrolling", "version": None,
             "last_heartbeat_at": None, "pending": True, "certificate_expires_at": None,
             "config_apply_error": None, "upd_state": None, "upd_error": None, "upd_target": None,
             "conflict": False},
        ][: p["lim"]]),
    ])


def test_agent_fleet_health_versions_issues_and_certificates(monkeypatch):
    db = _agents_db()
    sec = run(monkeypatch, "agent_fleet_health", db)
    assert kpi(sec, "Agents")["subtitle"] == "1 revoked not counted"
    assert kpi(sec, "Online")["subtitle"] == "60.0% of 10"
    assert kpi(sec, "Offline or stale")["value"] == "3"
    assert kpi(sec, "Offline or stale")["accent"] == "danger"
    assert kpi(sec, "Outdated")["value"] == "3"
    assert kpi(sec, "Outdated")["subtitle"] == "2 upgrade(s) in progress · 1 failed"
    cert = kpi(sec, "Certificates expiring ≤ 30 d")
    assert cert["value"] == "2" and cert["subtitle"] == "1 already expired" and cert["accent"] == "danger"
    assert db.call("AS attention")[1] == {"cert_days": 30}
    versions = table(sec, "Agent versions")
    assert versions["rows"] == [["Windows", "1.14.2", "5", "Latest"],
                                ["Windows", "1.13.0", "3", "Outdated (latest 1.14.2)"],
                                ["Linux", "1.10.0", "1", "Latest"],
                                ["Linux", "—", "1", "Not reported"]]
    t = table(sec, "Agents needing attention")
    issues = t["rows"][0][4]
    assert issues.startswith("No heartbeat since Oct 01 15:00")
    assert "Certificate expired" in issues and "Config error: invalid event log filter" in issues
    assert "Upgrade to 1.14.2 failed: signature mismatch" in issues
    assert t["rows"][1][:2] == ["new-host", "—"] and t["rows"][1][4] == "Pending authorization"
    assert t["title"] == "Agents needing attention (top 2 of 4)"
    status = [p["label"] for p in chart(sec, "Agents by status")["series"]["points"]]
    assert status == ["Online", "Enrolling", "Stale", "Offline"]


def test_agent_fleet_health_options_and_unreported_certificates(monkeypatch):
    db = _agents_db(cert_reported=0)
    sec = run(monkeypatch, "agent_fleet_health", db, cert_days=14, top_n=1)
    assert db.call("AS attention")[1]["cert_days"] == 14
    assert db.call("AS upd_state")[1] == {"cert_days": 14, "lim": 1}
    cert = kpi(sec, "Certificates expiring ≤ 14 d")
    assert cert["value"] == "—" and cert["subtitle"] == "Not reported by agents"
    assert any("not measured" in n for n in sec["notes"])


def test_agent_fleet_health_empty(monkeypatch):
    sec = run(monkeypatch, "agent_fleet_health", FakeDB())
    assert sec["notes"] == ["No agents are enrolled."]


# ── agent_overhead ──────────────────────────────────────────────────────────

def _overhead():
    ch = FakeCH([
        ("uniqExact(agent_id) AS agents", [(5, 1.234, 3.5, 12.0, 52428800.0, 104857600, 1500.0, 1048576, 12, 2,
                                            1000)]),
        ("GROUP BY agent_id", lambda sql, p: [
            ("a1", "s1", 4.5, 12.0, 60 * 1024 * 1024.0, 100 * 1024 * 1024, 12, 1048576, 2500.0, 2),
            ("a2", "s2", 0.8, 3.0, 40 * 1024 * 1024.0, 50 * 1024 * 1024, 0, 0, float("nan"), 0),
        ][: p["lim"]]),
        (("toUnixTimestamp(", "agent_health_metrics"), [(DAYS[0], 1.1, 5.0, 1200.0), (DAYS[1], 1.3, 12.0, None)]),
    ])
    db = FakeDB([("WHERE a.id::text = ANY(:ids)", [{"id": "a1", "agent": "WEB-01", "server": "web-01"},
                                                   {"id": "a2", "agent": "DB-01", "server": None}])])
    return db, ch


def test_agent_overhead_kpis_trend_and_ranking(monkeypatch):
    db, ch = _overhead()
    sec = run(monkeypatch, "agent_overhead", db, ch)
    assert kpi(sec, "Agents reporting")["value"] == "5"
    assert kpi(sec, "Agent CPU (average)")["value"] == "1.2%"
    assert kpi(sec, "Agent CPU (average)")["subtitle"] == "p95 3.5% · peak 12.0%"
    assert kpi(sec, "Agent memory (average)")["value"] == "50.0 MB"
    assert kpi(sec, "Upload lag (p95)")["value"] == "1.50 s"
    assert kpi(sec, "Largest spool backlog")["value"] == "1.0 MB"
    assert "ORDER BY cpu_avg DESC" in ch.call("GROUP BY agent_id")[0]
    t = table(sec, "Agents by CPU")
    assert t["title"] == "Agents by CPU (top 2 of 5)"
    assert t["rows"][0] == ["WEB-01", "web-01", "4.5%", "12.0%", "60.0 MB", "1.0 MB", "2.50 s"]
    assert t["rows"][1][1] == "—" and t["rows"][1][6] == "—"        # nan lag is not measured
    cpu = chart(sec, "Agent CPU")["series"]
    assert [s["name"] for s in cpu["series"]] == ["Average", "Peak"]
    assert cpu["series"][0]["points"][0] == {"t": _iso(DAYS[0]), "v": 1.1}
    lag = chart(sec, "Upload lag (p95)")["series"]
    assert [p["v"] for p in lag["points"]] == [1200.0, None]
    assert any("2 health sample(s)" in n for n in sec["notes"])


def test_agent_overhead_sort_option(monkeypatch):
    db, ch = _overhead()
    sec = run(monkeypatch, "agent_overhead", db, ch, sort="lag", top_n=1)
    sql, params = ch.call("GROUP BY agent_id")
    assert "ORDER BY lag_p95 DESC" in sql and params["lim"] == 1
    assert table(sec, "Agents by upload lag")["title"] == "Agents by upload lag (top 1 of 5)"


def test_agent_overhead_empty(monkeypatch):
    sec = run(monkeypatch, "agent_overhead", FakeDB(), FakeCH())
    assert sec["notes"][0] == "No agent health telemetry was received in this period."


# ── retention disclosures ───────────────────────────────────────────────────

@pytest.mark.parametrize("sid,needle", [
    ("server_event_log_errors", "Event log summary data is kept for 30 days"),
    ("agent_overhead", "Agent health telemetry is kept for 30 days"),
])
def test_windows_past_retention_are_disclosed(monkeypatch, sid, needle):
    frm = TO - timedelta(days=60)
    sec = run(monkeypatch, sid, FakeDB(), FakeCH(), frm=frm)
    assert any(n.startswith(needle) for n in sec["notes"]), sec["notes"]
