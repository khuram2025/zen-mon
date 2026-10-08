"""APM section pack (report_packs/apm).

Every section runs through report_sections.build_sections against fakes:
Postgres answers canned rows keyed by SQL substrings, ClickHouse likewise
(rows shaped as clickhouse_connect returns them). The window is the seven
local days before today in Asia/Riyadh (UTC+3, no DST), so day buckets, chart
timestamps and table times all prove the organisation timezone is honoured."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import apm
from tests.conftest import _NullTransaction

RIYADH = ZoneInfo("Asia/Riyadh")
BRAND = Branding(timezone="Asia/Riyadh")
_TODAY = datetime.now(RIYADH).replace(hour=0, minute=0, second=0, microsecond=0)
TO = _TODAY.astimezone(timezone.utc).replace(tzinfo=None)       # local midnight, naive UTC
FRM = TO - timedelta(days=7)
FRM_E = int(FRM.replace(tzinfo=timezone.utc).timestamp())
DAYS = [FRM_E + i * 86400 for i in range(7)]

SECTIONS = ("apm_latency_trend", "apm_slowest_operations", "apm_service_dependencies",
            "apm_slo_budget_trend", "apm_new_errors", "apm_release_health", "apm_db_queries",
            "apm_data_quality", "apm_instrumentation_coverage", "synthetics_failures")
TEMPLATES = ("apm_latency_throughput", "apm_slowest_operations_review",
             "apm_service_dependency_review", "apm_slo_error_budget", "apm_new_regressed_errors",
             "apm_release_health_review", "apm_top_database_queries", "apm_telemetry_ingest_quality",
             "apm_instrumentation_coverage_report", "apm_synthetic_journey_failures")


def local_short(dt: datetime) -> str:
    """'Oct 05 15:07' for a naive-UTC instant shown in Riyadh (UTC+3)."""
    return (dt + timedelta(hours=3)).strftime("%b %d %H:%M")


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


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

    def all(self, *needles: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if all(n in c[0] for n in needles)]


def run(monkeypatch, sid, db=None, ch=None, title=None, frm=FRM, to=TO, **options):
    db = db or FakeDB()
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
        assert k.get("accent") in ("primary", "success", "warning", "danger", "info", "neutral"), k
    for c in sec["charts"]:
        assert c["series"]["kind"] in ("area", "line", "bars", "stacked", "rank", "donut")
    return sec


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


def table(sec, prefix):
    return next(t for t in sec["tables"] if t["title"].startswith(prefix))


def chart(sec, prefix):
    return next(c for c in sec["charts"] if c["title"].startswith(prefix))


# ── registry and templates ──────────────────────────────────────────────────

def test_sections_are_registered_fleet_scoped_with_apm_permission():
    for sid in SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["fn"].__module__ == apm.__name__, sid
        assert e["scope"] == "fleet", sid
        assert "apm.view" in e["requires"], sid
        assert e["category"] == "Applications", sid
    assert rs.SECTION_REGISTRY["apm_instrumentation_coverage"]["requires"] == ("apm.view", "servers.view")
    assert not rs.may_view_section(frozenset({"apm.view"}), "apm_instrumentation_coverage")
    assert rs.may_view_section(frozenset({"apm.view", "servers.view"}), "apm_instrumentation_coverage")
    snapshots = {sid for sid in SECTIONS if rs.SECTION_REGISTRY[sid]["semantics"] == "snapshot"}
    assert snapshots == {"apm_instrumentation_coverage"}


def test_templates_are_valid_and_published():
    assert set(TEMPLATES) <= set(rt.TEMPLATES)
    assert rt.validate_catalog() == []
    featured = {k for k in TEMPLATES if rt.TEMPLATES[k].featured}
    assert featured == {"apm_slo_error_budget"}
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category == "applications", key
        assert t.audience and set(t.audience) <= set(rt.AUDIENCES), key
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
        assert "apm.view" in t.requires(), key
    covered = {sid for k in TEMPLATES for sid in rt.TEMPLATES[k].section_ids}
    assert set(SECTIONS) <= covered
    # Without servers.view the coverage template still offers its APM section.
    perms = frozenset({"apm.view"})
    tpl = rt.TEMPLATES["apm_instrumentation_coverage_report"]
    assert rt.is_available(tpl, perms)
    assert "apm_instrumentation_coverage" not in rt.available_sections(tpl, perms)


# ── masking ─────────────────────────────────────────────────────────────────

def test_mask_statement_replaces_literals_and_keeps_structure():
    assert apm.mask_statement(
        "SELECT * FROM users WHERE name = 'O''Brien' AND id = 42 AND x IN (1, 2, 3)"
    ) == "SELECT * FROM users WHERE name = ? AND id = ? AND x IN (?, …)"
    assert apm.mask_statement('SELECT "UserName" FROM "Users" WHERE "Id" = $1 AND t2.c = 0x1F') == \
        'SELECT "UserName" FROM "Users" WHERE "Id" = $1 AND t2.c = ?'
    assert apm.mask_statement('{"find": "users", "filter": {"email": "bob@x.com", "age": 41}}') == \
        '{"find": ?, "filter": {"email": ?, "age": ?}}'
    assert apm.mask_statement("UPDATE t SET a = 'unterminated") == "UPDATE t SET a = ?"
    assert apm.mask_statement("GET session:123456789") == "GET session:?"
    long = apm.mask_statement("SELECT " + ", ".join(f"col_{i}" for i in range(60)) + " FROM t")
    assert len(long) == 120 and long.endswith("…")
    assert apm.mask_statement("") == "—"


def test_mask_message_hides_credentials_and_identifiers():
    out = apm.mask_message("Login failed for bob@example.com password=hunter2 Bearer abcdef123456 "
                           "at /api?session=xyz&page=2 order 1234567890 id "
                           "3f2b8c1e-1111-2222-3333-444455556666")
    for secret in ("bob@example.com", "hunter2", "abcdef123456", "xyz", "1234567890", "3f2b8c1e"):
        assert secret not in out
    assert out.startswith("Login failed for ? password=? Bearer ? at /api?session=?&page=?")
    assert apm.mask_message("KeyError: 'user_id'") == "KeyError: 'user_id'"


# ── apm_latency_trend ───────────────────────────────────────────────────────

def _latency_ch():
    def totals(sql, p):
        if p["f"] >= FRM:
            return [(12000, 150, [42.1, 480.0], 11000, 600, 4)]
        return [(10000, 100, [40.0, 400.0], 9000, 800, 3)]

    return FakeCH([
        (("AS b, sum(request_count)", "apm_span_metrics_1h"), [
            (FRM_E, 7200, 72, [40.0, 450.0]),
            (FRM_E + 7200, 3600, 72, [45.0, 520.0]),
            (FRM_E + 6 * 3600, 1200, 6, [41.0, 470.0]),
        ]),
        ("uniqExact(service_name) FROM zenplus.apm_span_metrics", totals),
    ])


def test_latency_trend_kpis_series_gaps_and_previous_period(monkeypatch):
    ch = _latency_ch()
    sec = run(monkeypatch, "apm_latency_trend", ch=ch)
    assert kpi(sec, "Requests")["value"] == "12,000"
    assert kpi(sec, "Requests")["subtitle"] == "▲ 20.0% vs previous period"
    err = kpi(sec, "Error rate")
    assert (err["value"], err["accent"], err["subtitle"]) == \
        ("1.25%", "warning", "▲ 0.25 pts vs previous period")
    assert kpi(sec, "Median response time")["value"] == "42.1 ms"
    p95 = kpi(sec, "P95 response time")
    assert (p95["value"], p95["subtitle"]) == ("480 ms", "▲ 20.0% vs previous period")
    assert kpi(sec, "Apdex")["value"] == "0.94"
    assert kpi(sec, "Services reporting")["value"] == "4"

    sql, p = ch.call("AS b, sum(request_count)")
    # A 7-day window reads the hourly rollup in 2-hour buckets aligned to Riyadh.
    assert "INTERVAL 2 HOUR" in sql and p["org_tz"] == "Asia/Riyadh"
    assert "span_kind IN ('SERVER','CONSUMER')" in sql
    assert p["f"] == FRM and p["t"] == TO
    lat = chart(sec, "Response time")["series"]
    assert [s["name"] for s in lat["series"]] == ["Median", "95th percentile"]
    pts = lat["series"][1]["points"]
    assert [x["t"] for x in pts] == [iso(FRM_E), iso(FRM_E + 7200), iso(FRM_E + 4 * 3600),
                                     iso(FRM_E + 6 * 3600)]
    assert [x["v"] for x in pts] == [450.0, 520.0, None, 470.0]      # a gap, never zero
    rpm = chart(sec, "Throughput")["series"]["points"]
    assert rpm[0]["v"] == 60.0 and rpm[2]["v"] is None
    assert chart(sec, "Error rate")["series"]["points"][0]["v"] == 1.0
    assert any("2-hour interval" in n for n in sec["notes"])


def test_latency_trend_percentile_option_and_short_window_table(monkeypatch):
    ch = _latency_ch()
    sec = run(monkeypatch, "apm_latency_trend", ch=ch, percentile="p99")
    assert "quantilesTDigestMerge(0.5, 0.99)" in ch.call("AS b, sum(request_count)")[0]
    assert kpi(sec, "P99 response time")["value"] == "480 ms"
    assert "99th percentile" in sec["description"]

    ch = FakeCH()
    to = datetime.now(timezone.utc).replace(tzinfo=None)
    sec = run(monkeypatch, "apm_latency_trend", ch=ch, frm=to - timedelta(hours=3), to=to)
    sql, p = ch.call("AS b, sum(request_count)")
    assert "apm_span_metrics_5m" in sql and "INTERVAL 5 MINUTE" in sql
    assert p["f"].minute % 5 == 0 and p["f"].second == 0          # bucket-aligned start
    assert sec["notes"][0].startswith("No inbound requests")


# ── apm_slowest_operations ──────────────────────────────────────────────────

def _ops_ch():
    return FakeCH([
        ("countIf(c >= %(min_calls)s)", [(5, 2, 9000, 1_800_000.0)]),
        ("GROUP BY service_name, operation HAVING calls", lambda sql, p: [
            ("checkout", "POST /checkout", 5000, 50, [40.0, 180.0, 400.0], 900_000.0),
            ("payments", "charge", 2000, 100, [200.0, 700.0, 1500.0], 600_000.0),
        ][: p["lim"]]),
    ])


def test_slowest_operations_share_of_time_and_formatting(monkeypatch):
    sec = run(monkeypatch, "apm_slowest_operations", ch=_ops_ch())
    assert sec["title"] == "Top 15 Operations by Total Time"
    t = table(sec, "Operations")
    assert t["title"] == "Operations"
    assert t["rows"][0] == ["checkout", "POST /checkout", "5,000", "180 ms", "180 ms", "15.0 min",
                            "50.00%", "1.00%"]
    assert t["rows"][1] == ["payments", "charge", "2,000", "300 ms", "700 ms", "10.0 min", "33.33%",
                            "5.00%"]
    assert kpi(sec, "Operations")["subtitle"] == "2 with at least 10 calls"
    assert kpi(sec, "Share of time in the listed 2")["value"] == "83.33%"
    assert chart(sec, "Share of server time")["series"]["points"][0] == \
        {"label": "checkout · POST /checkout", "value": 50.0}


def test_slowest_operations_options(monkeypatch):
    ch = _ops_ch()
    sec = run(monkeypatch, "apm_slowest_operations", ch=ch, top_n=1, sort="p95", min_calls=500)
    sql, p = ch.call("HAVING calls")
    assert "ORDER BY arrayElement(q, 2) DESC" in sql
    assert (p["lim"], p["min_calls"]) == (1, 500)
    assert sec["title"] == "Top 1 Operations by 95th-Percentile Latency"
    assert table(sec, "Operations")["title"] == "Operations (top 1 of 2)"
    assert chart(sec, "95th-percentile latency")["series"]["unit"] == "ms"

    sec = run(monkeypatch, "apm_slowest_operations", ch=FakeCH([
        ("countIf(c >= %(min_calls)s)", [(5, 0, 9000, 1_800_000.0)])]))
    assert sec["notes"][0] == "No operation reached 10 calls in this period."


# ── apm_service_dependencies ────────────────────────────────────────────────

def _deps_ch():
    return FakeCH([
        ("arrayConcat", [(2, 282_240, 4032, 7_740_000.0, 3)]),
        ("GROUP BY client_service, server_service HAVING calls", [
            ("web", "checkout", 201_600, 0, 6_048_000.0),
            ("checkout", "payments", 80_640, 4032, 18_144_000.0),
        ]),
    ])


def test_service_dependencies_table_status_and_kpis(monkeypatch):
    ch = _deps_ch()
    sec = run(monkeypatch, "apm_service_dependencies", ch=ch)
    t = table(sec, "Dependencies")
    assert t["rows"][0] == ["web", "checkout", "201,600", "20.0", "0.00%", "30.0 ms", "1.7 h",
                            "healthy"]
    assert t["rows"][1] == ["checkout", "payments", "80,640", "8.0", "5.00%", "225 ms", "5.0 h",
                            "critical"]
    assert kpi(sec, "Services connected")["value"] == "3"
    assert kpi(sec, "Error rate")["value"] == "1.43%"
    slow = kpi(sec, "Slowest listed dependency")
    assert (slow["value"], slow["subtitle"]) == ("225 ms", "checkout → payments")
    _sql, p = ch.call("HAVING calls")
    assert p["f"] == FRM and p["t"] == TO and p["lim"] == 20


def test_service_dependencies_sort_option_and_empty(monkeypatch):
    ch = _deps_ch()
    sec = run(monkeypatch, "apm_service_dependencies", ch=ch, sort="error_rate", top_n=5)
    sql, p = ch.call("HAVING calls")
    assert "ORDER BY errs / calls DESC" in sql and p["lim"] == 5
    assert sec["title"] == "Service Dependencies by Error Rate"
    assert chart(sec, "Error rate by dependency")["series"]["unit"] == "%"
    sec = run(monkeypatch, "apm_service_dependencies")
    assert sec["notes"][0] == "No calls between instrumented services in this period."


# ── apm_slo_budget_trend ────────────────────────────────────────────────────

GRID = [10, 20, 30, 50, 80, 100, 120, 150, 200, 250, 300, 400, 500, 600, 700, 900]


def _slo_db():
    return FakeDB([("FROM apm_slos s", [
        {"id": "a", "name": "Checkout availability", "operation": None, "sli_type": "availability",
         "latency_threshold_ms": None, "target": 99.9, "window_days": 7, "service_name": "checkout",
         "env": "prod"},
        {"id": "b", "name": "Payments latency", "operation": "charge", "sli_type": "latency",
         "latency_threshold_ms": 300, "target": 95.0, "window_days": 30, "service_name": "payments",
         "env": None},
        {"id": "c", "name": "Idle availability", "operation": None, "sli_type": "availability",
         "latency_threshold_ms": None, "target": 99.0, "window_days": 30, "service_name": "idle",
         "env": None},
        {"id": "d", "name": "Custom journey", "operation": None, "sli_type": "custom",
         "latency_threshold_ms": None, "target": 99.0, "window_days": 30, "service_name": "web",
         "env": None},
    ])])


def _slo_ch():
    def rows(sql, p):
        if p["svc"] == "checkout":
            return [(DAYS[4], 10000, 2), (DAYS[5], 10000, 3), (DAYS[6], 10000, 5)]
        if p["svc"] == "payments":
            return [(DAYS[6], 1000, GRID)]
        return []
    return FakeCH([("FROM zenplus.apm_span_metrics_1h", rows)])


def test_slo_budget_trend_status_budget_and_no_data(monkeypatch):
    ch = _slo_ch()
    sec = run(monkeypatch, "apm_slo_budget_trend", db=_slo_db(), ch=ch)
    rows = table(sec, "Service level objectives")["rows"]
    assert rows[0] == ["Payments latency", "payments", "95.00% under 300 ms · 30d", "90.00%",
                       "200.00%", "0.00%", "breaching"]
    assert rows[1] == ["Checkout availability", "checkout", "99.90% available · 7d", "99.97%",
                       "33.33%", "66.67%", "on track"]
    # No requests is "no data", never a full budget.
    assert rows[2] == ["Custom journey", "web", "99.00% (custom SLI) · 30d", "—", "—", "—", "no data"]
    assert rows[3][-3:] == ["—", "—", "no data"]
    assert [kpi(sec, k)["value"] for k in ("SLOs", "On track", "At risk", "Breaching", "No data")] == \
        ["4", "1", "0", "1", "2"]
    budget = chart(sec, "Error budget remaining")["series"]
    assert budget["y_domain"] == [0, 100]
    assert [s["name"] for s in budget["series"]] == ["Payments latency", "Checkout availability"]
    assert [(p["t"], p["v"]) for p in budget["series"][1]["points"]] == \
        [(iso(DAYS[4]), 80.0), (iso(DAYS[5]), 75.0), (iso(DAYS[6]), 66.67)]
    assert budget["series"][0]["points"][0]["v"] == 0.0       # overspent stops at 0 in the chart
    sli = chart(sec, "Daily SLI vs target — Payments latency")["series"]
    assert [s["name"] for s in sli["series"]] == ["SLI", "Target"]

    sql, p = ch.call("service_name = %(svc)s", "sum(error_count)")
    assert "env = %(env)s" in sql and p["env"] == "prod" and p["org_tz"] == "Asia/Riyadh"
    assert p["t"] == TO and p["f"] == TO - timedelta(days=7)
    lat_sql, lat_p = ch.call("quantilesTDigestMerge(0.01")
    assert "operation = %(op)s" in lat_sql and lat_p["op"] == "charge"
    assert "env = %(env)s" not in lat_sql
    assert not any(p.get("svc") == "web" for _s, p in ch.calls)      # custom SLIs are not queried
    assert any("trailing window" in n and "not over the report period" in n for n in sec["notes"])
    assert any("1 custom SLO" in n for n in sec["notes"])


def test_slo_budget_trend_top_n_and_empty(monkeypatch):
    sec = run(monkeypatch, "apm_slo_budget_trend", db=_slo_db(), ch=_slo_ch(), top_n=1)
    t = table(sec, "Service level objectives")
    assert t["title"] == "Service level objectives (top 1 of 4)" and len(t["rows"]) == 1
    sec = run(monkeypatch, "apm_slo_budget_trend")
    assert sec["notes"] == ["No service level objectives are defined."]


# ── apm_new_errors ──────────────────────────────────────────────────────────

RESOLVED_AT = FRM + timedelta(days=1)
NEW_FIRST = FRM + timedelta(days=2, hours=12, minutes=7)


def _err_fixtures():
    gid = {k: k.encode() * 16 for k in "abcdef"}         # FixedString(16) comes back as bytes
    ch = FakeCH([("FROM zenplus.apm_exceptions", [
        (gid["a"], "KeyError", "User bob@example.com not found (order 12345678)", "checkout",
         NEW_FIRST, 9, NEW_FIRST + timedelta(hours=5), [("5.1", 9, NEW_FIRST + timedelta(hours=5))]),
        (gid["b"], "TimeoutError", "upstream timeout", "checkout", FRM - timedelta(days=3), 4,
         FRM + timedelta(days=1), [("5.1", 4, FRM + timedelta(days=1))]),
        (gid["c"], "ValueError", "bad amount", "payments", FRM - timedelta(days=10), 7,
         FRM + timedelta(days=3), [("2.4.1", 5, FRM + timedelta(days=3)),
                                   ("2.3.9", 2, FRM + timedelta(days=2))]),
        (gid["d"], "ValueError", "old instance", "payments", FRM - timedelta(days=10), 3,
         FRM + timedelta(days=3), [("2.3.0", 3, FRM + timedelta(days=3))]),
        (gid["e"], "NoiseError", "ignored", "web", FRM + timedelta(days=1), 50,
         FRM + timedelta(days=2), [("1.0", 50, FRM + timedelta(days=2))]),
        (gid["f"], "IOError", "disk", "web", FRM - timedelta(days=9), 2,
         FRM + timedelta(hours=10), [("1.0", 2, FRM + timedelta(hours=10))]),
    ])])
    db = FakeDB([("FROM apm_error_issues", [
        {"group_id": "c" * 16, "status": "resolved_in_version", "resolved_in_version": "2.4.0",
         "updated_at": utc(RESOLVED_AT)},
        {"group_id": "d" * 16, "status": "resolved_in_version", "resolved_in_version": "2.4.0",
         "updated_at": utc(RESOLVED_AT)},
        {"group_id": "e" * 16, "status": "ignored", "resolved_in_version": None, "updated_at": utc(FRM)},
        {"group_id": "f" * 16, "status": "resolved", "resolved_in_version": None,
         "updated_at": utc(FRM + timedelta(days=1))},
    ])])
    return db, ch


def test_new_errors_new_regressed_and_ignored(monkeypatch):
    db, ch = _err_fixtures()
    sec = run(monkeypatch, "apm_new_errors", db=db, ch=ch)
    assert [kpi(sec, k)["value"] for k in ("New error groups", "Regressed", "Occurrences",
                                           "Services affected")] == ["1", "1", "16", "2"]
    new = table(sec, "New error groups")
    assert new["rows"] == [["KeyError", "User ? not found (order ?)", "checkout", "9",
                            local_short(NEW_FIRST), local_short(NEW_FIRST + timedelta(hours=5)),
                            "unresolved"]]
    reg = table(sec, "Regressed error groups")
    assert reg["rows"] == [["ValueError", "bad amount", "payments",
                            f"{local_short(RESOLVED_AT)} (in 2.4.0)", "2.4.1", "7",
                            local_short(FRM + timedelta(days=3))]]
    bars = chart(sec, "New error groups by day")["series"]["points"]
    assert [p["t"] for p in bars] == [iso(d) for d in DAYS]
    assert [p["v"] for p in bars] == [0, 0, 1, 0, 0, 0, 0]
    assert "1 ignored error group(s) are excluded." in sec["notes"]
    _sql, p = db.call("FROM apm_error_issues")
    assert sorted(p["gids"]) == [k * 16 for k in "abcdef"]
    sql, p = ch.call("FROM zenplus.apm_exceptions")
    assert p["f"] == FRM and p["t"] == TO and p["lb"] == FRM - timedelta(days=30)


def test_new_errors_include_option_and_top_n(monkeypatch):
    db, ch = _err_fixtures()
    sec = run(monkeypatch, "apm_new_errors", db=db, ch=ch, include="new")
    assert sec["title"] == "New Errors"
    assert [t["title"] for t in sec["tables"]] == ["New error groups"]
    assert "Regressed" not in [k["label"] for k in sec["kpis"]]
    db, ch = _err_fixtures()
    sec = run(monkeypatch, "apm_new_errors", db=db, ch=ch, include="regressed")
    assert [t["title"] for t in sec["tables"]] == ["Regressed error groups"]
    assert not sec["charts"]
    sec = run(monkeypatch, "apm_new_errors")
    assert sec["notes"][0] == "No new or regressed errors in this period."


# ── apm_release_health ──────────────────────────────────────────────────────

PAY_AT = FRM + timedelta(days=4, hours=3, minutes=7)
CHK_AT = FRM + timedelta(days=2)
DET_AT = FRM + timedelta(days=5, minutes=2)


def _release_fixtures():
    db = FakeDB([("FROM apm_deployments d", [
        {"id": "d1", "service": "payments", "env": "prod", "version": "2.0.0",
         "git_sha": "abcdef123456", "deployed_at": utc(PAY_AT)},
        {"id": "d2", "service": "checkout", "env": None, "version": "5.1", "git_sha": None,
         "deployed_at": utc(CHK_AT)},
    ])])

    def phases(sql, p):
        if p["svc"] == "payments":
            return [(0, 1000, 10, [400.0]), (1, 1000, 60, [900.0])]
        if p["d0"] == CHK_AT:
            return [(0, 1000, 5, [100.0]), (1, 1000, 6, [105.0])]
        return [(0, 800, 2, [90.0])]

    ch = FakeCH([
        (("FROM zenplus.apm_spans", "service_version != ''"), [
            ("checkout", "", "5.2", DET_AT), ("payments", "prod", "2.0.0", PAY_AT)]),
        ("AS is_after", phases),
    ])
    return db, ch


def test_release_health_before_after_verdicts(monkeypatch):
    db, ch = _release_fixtures()
    sec = run(monkeypatch, "apm_release_health", db=db, ch=ch)
    t = table(sec, "Deployments, most recent first")
    assert t["rows"] == [
        ["checkout", "5.2", local_short(DET_AT), "Detected", "0.25% → —", "90.0 ms → —", "—",
         "no data"],
        ["payments", "2.0.0 (abcdef1)", local_short(PAY_AT), "Recorded", "1.00% → 6.00%",
         "400 ms → 900 ms", "1,000", "critical"],
        ["checkout", "5.1", local_short(CHK_AT), "Recorded", "0.50% → 0.60%", "100 ms → 105 ms",
         "1,000", "healthy"],
    ]
    assert [kpi(sec, k)["value"] for k in ("Deployments", "Services deployed", "Releases that degraded",
                                           "Not enough traffic to compare")] == ["3", "2", "1", "1"]
    bars = chart(sec, "Deployments per day")["series"]["points"]
    assert [p["v"] for p in bars] == [0, 0, 1, 0, 1, 1, 0]
    # The 5-minute bucket holding the deployment is excluded and the window
    # stops at the next deployment of the same service.
    chk = next(p for _s, p in ch.all("AS is_after") if p["d0"] == CHK_AT)
    assert chk["b0"] == CHK_AT - timedelta(hours=24)
    assert chk["a1"] == CHK_AT + timedelta(hours=24, minutes=5)
    pay_sql, pay = next(c for c in ch.all("AS is_after") if c[1]["svc"] == "payments")
    assert pay["d0"] == PAY_AT.replace(minute=5) and "env = %(env)s" in pay_sql
    _sql, p = db.call("FROM apm_deployments d")
    assert p["f"] == utc(FRM) and p["t"] == utc(TO)


def test_release_health_options(monkeypatch):
    db, ch = _release_fixtures()
    sec = run(monkeypatch, "apm_release_health", db=db, ch=ch, detect_versions=False, compare_hours=6,
              top_n=1)
    assert not ch.all("FROM zenplus.apm_spans")
    rows = table(sec, "Deployments")["rows"]
    assert len(rows) == 1 and rows[0][0] == "payments"
    _s, p = ch.call("AS is_after")
    assert p["b0"] == PAY_AT.replace(minute=5) - timedelta(hours=6)
    assert "6 hour(s)" in sec["description"]
    sec = run(monkeypatch, "apm_release_health")
    assert sec["notes"][0] == "No deployments were recorded or detected in this period."


# ── apm_db_queries ──────────────────────────────────────────────────────────

RAW_STMT = ("SELECT id, email FROM users WHERE token = 'abc-secret' AND tenant = 42 AND mail = "
            "bob@x.com AND " + " AND ".join(f"flag_{i} = 1" for i in range(20)))


def _db_ch():
    return FakeCH([
        ("uniqExact(normalizedQueryHash(db_statement))", [(2000, 50, 60_000.0, 7)]),
        ("GROUP BY h ORDER BY", lambda sql, p: [
            (123, RAW_STMT, "postgresql", "SELECT", ["api", "worker", "billing"], 5, 1000, 20, 12.5,
             30_000.0),
            (456, "INSERT INTO audit VALUES (?..)", "postgresql", "INSERT", ["api"], 1, 600, 0, 3.2,
             6_000.0),
        ][: p["lim"]]),
    ])


def test_db_queries_masks_statements_and_ranks(monkeypatch):
    ch = _db_ch()
    sec = run(monkeypatch, "apm_db_queries", ch=ch)
    t = table(sec, "Statements")
    stmt = t["rows"][0][0]
    for raw in ("abc-secret", "42", "bob@x.com"):
        assert raw not in stmt
    assert stmt.startswith("SELECT id, email FROM users WHERE token = ? AND tenant = ? AND mail = ?")
    assert len(stmt) <= 120 and stmt.endswith("…")
    assert t["rows"][0][1:] == ["postgresql from api, worker, billing +2", "1,000", "12.5 ms",
                                "30.0 s", "50.00%", "2.00%"]
    assert t["rows"][1][1] == "postgresql from api"
    assert t["title"] == "Statements (top 2 of 7)"
    assert kpi(sec, "Database time")["value"] == "1.0 min"
    assert kpi(sec, "Top statement share")["value"] == "50.00%"
    assert kpi(sec, "Error rate")["value"] == "2.50%"
    sql, p = ch.call("GROUP BY h ORDER BY")
    assert "normalizeQuery(any(db_statement))" in sql and "ORDER BY total_ms DESC" in sql
    assert p["f"] == FRM and p["t"] == TO


def test_db_queries_sort_option_and_raw_retention(monkeypatch):
    ch = _db_ch()
    sec = run(monkeypatch, "apm_db_queries", ch=ch, sort="p95", top_n=1)
    sql, p = ch.call("GROUP BY h ORDER BY")
    assert "ORDER BY p95 DESC" in sql and p["lim"] == 1
    assert sec["title"] == "Top 1 Database Queries by 95th-Percentile Latency"
    assert "Top statement share" not in [k["label"] for k in sec["kpis"]]
    ch = _db_ch()
    sec = run(monkeypatch, "apm_db_queries", ch=ch, frm=TO - timedelta(days=30))
    _sql, p = ch.call("GROUP BY h ORDER BY")
    assert p["f"] >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14, minutes=1)
    assert any("kept for 14 days" in n for n in sec["notes"])


# ── apm_data_quality ────────────────────────────────────────────────────────

def _dq_fixtures():
    now = datetime.now(timezone.utc)
    ch = FakeCH([("FROM zenplus.apm_ingest_stats", [
        (DAYS[0], 1000, 5, 1, 2), (DAYS[2], 2000, 0, 0, 0)])])
    db = FakeDB([("FROM apm_services s", [
        {"name": "never", "env": "dev", "health": "no_data", "last_seen_at": None},
        {"name": "legacy", "env": None, "health": "no_data", "last_seen_at": now - timedelta(days=3)},
        {"name": "checkout", "env": "prod", "health": "healthy",
         "last_seen_at": now - timedelta(minutes=5)},
    ])])
    return db, ch


def test_data_quality_daily_counters_gaps_and_silent_services(monkeypatch):
    db, ch = _dq_fixtures()
    sec = run(monkeypatch, "apm_data_quality", db=db, ch=ch)
    assert kpi(sec, "Spans accepted")["value"] == "3,000"
    rej = kpi(sec, "Rejected")
    assert (rej["value"], rej["subtitle"]) == ("5", "0.17% of received · 2 clock skew")
    assert (kpi(sec, "Dropped on write")["value"], kpi(sec, "Dropped on write")["accent"]) == ("1", "danger")
    assert kpi(sec, "Silent services")["value"] == "2 of 3"
    acc = chart(sec, "Spans accepted per day")["series"]["points"]
    assert [p["t"] for p in acc] == [iso(d) for d in DAYS]
    assert [p["v"] for p in acc] == [1000, None, 2000, None, None, None, None]
    stacked = chart(sec, "Rejected and dropped")["series"]
    assert [s["name"] for s in stacked["series"]] == ["Rejected — clock skew", "Rejected — other",
                                                      "Dropped"]
    assert [s["points"][0]["v"] for s in stacked["series"]] == [2, 3, 1]
    daily = table(sec, "Ingest by day")["rows"]
    day0 = (datetime.fromtimestamp(DAYS[0], tz=RIYADH)).strftime("%Y-%m-%d")
    assert daily[-1] == [day0, "1,000", "5", "2", "1", "0.50%"]
    assert daily[-2][1:] == ["—"] * 5
    silent = table(sec, "Services silent for 24 hour(s) or more")["rows"]
    assert [r[0] for r in silent] == ["never", "legacy"]
    assert silent[0][2:] == ["Never", "—", "no data"]
    assert silent[1][1] == "—" and silent[1][3] == "3d 0h"
    sql, p = ch.call("FROM zenplus.apm_ingest_stats")
    assert "INTERVAL 1 DAY" in sql and p["org_tz"] == "Asia/Riyadh"


def test_data_quality_silent_hours_option_and_empty(monkeypatch):
    db, ch = _dq_fixtures()
    sec = run(monkeypatch, "apm_data_quality", db=db, ch=ch, silent_hours=96, top_n=5)
    assert kpi(sec, "Silent services")["value"] == "1 of 3"
    assert [r[0] for r in table(sec, "Services silent for 96")["rows"]] == ["never"]
    sec = run(monkeypatch, "apm_data_quality")
    assert sec["notes"][0].startswith("No spans were received")


# ── apm_instrumentation_coverage ────────────────────────────────────────────

def _cov_db(attention=None):
    return FakeDB([
        ("GROUP BY p.runtime", [
            {"runtime": "dotnet", "state": "active", "n": 4, "otel": 1},
            {"runtime": "dotnet", "state": "failed", "n": 1, "otel": 0},
            {"runtime": "java", "state": "none", "n": 2, "otel": 0},
            {"runtime": "iis", "state": "unsupported", "n": 1, "otel": 0},
            {"runtime": "other", "state": "none", "n": 3, "otel": 0},
        ]),
        ("COUNT(DISTINCT p.server_id)", [{"servers": 3}]),
        ("p.runtime_version", lambda sql, p: (attention if attention is not None else [
            {"server": "app-01", "runtime": "dotnet", "runtime_version": "8.0.4", "name": "",
             "exe_path": r"C:\Apps\Orders\Orders.Api.exe", "state": "failed", "otel_detected": False,
             "last_seen_at": utc(FRM + timedelta(days=6))},
            {"server": "app-02", "runtime": "java", "runtime_version": "", "name": "Billing",
             "exe_path": "/opt/billing/bin/java", "state": "none", "otel_detected": True,
             "last_seen_at": utc(FRM + timedelta(days=5))},
        ])[: p["lim"]]),
    ])


def test_instrumentation_coverage_matrix_and_attention(monkeypatch):
    db = _cov_db()
    sec = run(monkeypatch, "apm_instrumentation_coverage", db=db)
    assert kpi(sec, "Processes")["value"] == "11"
    assert kpi(sec, "Processes")["subtitle"] == "On 3 server(s)"
    inst = kpi(sec, "Instrumented")
    assert (inst["value"], inst["subtitle"]) == ("4", "57.1% coverage")
    assert kpi(sec, "Failed")["accent"] == "danger"
    m = table(sec, "Processes by runtime")["rows"]
    assert m[0] == [".NET", "5", "4", "0", "1", "0", "1", "80.0%"]
    assert m[-1] == ["Other", "3", "0", "0", "0", "3", "0", "—"]
    assert ["IIS", "1", "0", "0", "0", "0", "0", "—"] in m
    assert any(n.startswith("1 process(es) run a runtime version that cannot") for n in sec["notes"])
    att = table(sec, "Processes that failed")
    assert att["title"].endswith("(top 2 of 3)")
    assert att["rows"][0] == ["app-01", "Orders.Api.exe", ".NET 8.0.4", "failed", "no",
                              local_short(FRM + timedelta(days=6))]
    assert att["rows"][1][:5] == ["app-02", "Billing", "Java", "not instrumented", "yes"]
    donut = chart(sec, "Processes by instrumentation state")["series"]
    assert donut["kind"] == "donut" and donut["points"][0] == {"label": "Instrumented", "value": 4}
    assert all("cmdline" not in sql for sql, _p in db.calls)
    _sql, p = db.call("GROUP BY p.runtime")
    assert abs((datetime.now(timezone.utc) - p["since"]).total_seconds() - 7 * 86400) < 60


def test_instrumentation_coverage_options_and_empty(monkeypatch):
    db = _cov_db()
    sec = run(monkeypatch, "apm_instrumentation_coverage", db=db, active_days=30, top_n=1)
    _sql, p = db.call("GROUP BY p.runtime")
    assert abs((datetime.now(timezone.utc) - p["since"]).total_seconds() - 30 * 86400) < 60
    assert db.call("p.runtime_version")[1]["lim"] == 1
    assert table(sec, "Processes that failed")["title"].endswith("(top 1 of 3)")
    sec = run(monkeypatch, "apm_instrumentation_coverage")
    assert sec["notes"][0].startswith("No application processes were reported")


# ── synthetics_failures ─────────────────────────────────────────────────────

M1, M2, M3 = (str(uuid.UUID(int=i)) for i in (11, 12, 13))
LAST_FAIL = FRM + timedelta(days=3, hours=9, minutes=30)


def _syn_fixtures():
    db = FakeDB([("FROM apm_synthetic_monitors", [
        {"id": M1, "name": "Login journey", "monitor_type": "browser", "enabled": True},
        {"id": M2, "name": "Health API", "monitor_type": "api", "enabled": True},
        {"id": M3, "name": "Checkout", "monitor_type": "api", "enabled": False},
    ])])
    ch = FakeCH([
        ("AS b, count(), countIf(success = 1)", [
            (DAYS[0], 48, 46), (DAYS[1], 48, 48), (DAYS[3], 48, 45)]),
        ("GROUP BY monitor_id ORDER BY failed", [
            (uuid.UUID(M1), 96, 5, 850.4, LAST_FAIL),
            (uuid.UUID(M2), 48, 0, 120.0, datetime(1970, 1, 1))]),
        ("GROUP BY monitor_id, failed_step", lambda sql, p: [
            (uuid.UUID(M1), "login", 4), (uuid.UUID(M1), "", 1)][: p["lim"]]),
        ("GROUP BY error", [
            ("HTTP 503 from https://app/api?token=abc123", 3, 1, LAST_FAIL),
            ("HTTP 503 from https://app/api?token=zzz999", 1, 1, FRM + timedelta(days=1)),
            ("timeout after 30s", 1, 1, FRM + timedelta(days=2))]),
    ])
    return db, ch


def test_synthetics_failures_rates_steps_and_errors(monkeypatch):
    db, ch = _syn_fixtures()
    sec = run(monkeypatch, "synthetics_failures", db=db, ch=ch)
    assert kpi(sec, "Runs")["value"] == "144"
    assert kpi(sec, "Runs")["subtitle"] == "2 of 3 scenario(s) ran"
    assert kpi(sec, "Success rate")["value"] == "96.53%"
    assert kpi(sec, "Failed runs")["value"] == "5"
    assert kpi(sec, "Scenarios with failures")["value"] == "1 of 2"
    bars = chart(sec, "Success rate per day")["series"]
    assert bars["y_domain"] == [0, 100]
    assert [p["t"] for p in bars["points"]] == [iso(d) for d in DAYS]
    assert [p["v"] for p in bars["points"]] == [95.83, 100.0, None, 93.75, None, None, None]
    assert table(sec, "Scenarios with failed runs")["rows"] == [
        ["Login journey", "BROWSER", "96", "5", "94.79%", "850 ms", local_short(LAST_FAIL)]]
    assert table(sec, "Most frequent failing steps")["rows"] == [
        ["Login journey", "login", "4", "80.00%"], ["Login journey", "(scenario)", "1", "20.00%"]]
    errors = table(sec, "Most frequent errors")["rows"]
    assert errors[0] == ["HTTP 503 from https://app/api?token=?", "4", "1", local_short(LAST_FAIL)]
    assert all("abc123" not in r[0] and "zzz999" not in r[0] for r in errors)
    assert "1 scenario(s) ran without a failed run." in sec["notes"]
    _sql, p = ch.call("GROUP BY monitor_id ORDER BY failed")
    assert sorted(p["ids"]) == sorted(uuid.UUID(m) for m in (M1, M2, M3))
    sql, p = ch.call("AS b, count(), countIf(success = 1)")
    assert "INTERVAL 1 DAY" in sql and p["org_tz"] == "Asia/Riyadh"


def test_synthetics_failures_top_n_and_empty_states(monkeypatch):
    db, ch = _syn_fixtures()
    sec = run(monkeypatch, "synthetics_failures", db=db, ch=ch, top_n=1)
    assert ch.call("GROUP BY monitor_id, failed_step")[1]["lim"] == 1
    assert len(table(sec, "Most frequent failing steps")["rows"]) == 1
    assert len(table(sec, "Most frequent errors")["rows"]) == 1
    sec = run(monkeypatch, "synthetics_failures")
    assert sec["notes"] == ["No synthetic scenarios are configured."]
    db, _ch = _syn_fixtures()
    sec = run(monkeypatch, "synthetics_failures", db=db, ch=FakeCH())
    assert sec["notes"][0] == "No synthetic runs were recorded in this period."


# ── retention disclosures ───────────────────────────────────────────────────

@pytest.mark.parametrize("sid,days,needle", [
    ("apm_new_errors", 40, "kept for 30 days"),
    ("apm_db_queries", 20, "kept for 14 days"),
    ("apm_data_quality", 40, "kept for 30 days"),
    ("apm_service_dependencies", 100, "kept for 90 days"),
    ("apm_latency_trend", 400, "kept for 395 days"),
])
def test_windows_past_retention_are_disclosed(monkeypatch, sid, days, needle):
    sec = run(monkeypatch, sid, frm=TO - timedelta(days=days))
    assert any(needle in n for n in sec["notes"]), sec["notes"]
