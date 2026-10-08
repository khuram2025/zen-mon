"""Service-check section pack (report_packs/services.py) with canned data.

Postgres and ClickHouse are fakes answering by SQL substring; nothing here
connects to a database. The confirmed-basis evidence runs through the real
service_availability code (read_fleet_segments -> summarize / incident
intervals / daily summaries) over fake probes and status-log events, so the
tests pin what the report shows: only confirmed incidents are downtime,
isolated failed probes are diagnostics, gaps are coverage, maintenance is
excluded, availability is two-decimal truncated and calendar days follow the
organisation timezone (Asia/Riyadh).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import services as pack

UTC = timezone.utc
RIYADH = ZoneInfo("Asia/Riyadh")
BRAND = Branding(timezone="Asia/Riyadh")

# Three whole local (Riyadh) days ending at today's local midnight.
_TODAY = datetime.now(RIYADH).replace(hour=0, minute=0, second=0, microsecond=0)
TO = _TODAY.astimezone(UTC).replace(tzinfo=None)          # naive UTC, like the render API
FRM = TO - timedelta(days=3)
SPAN = 3 * 86400
INTERVAL = 300

A, B, C, D, E, F = (f"{c * 8}-0000-0000-0000-00000000000{c}" for c in "abcdef")
G_CUSTOMER, G_PAYMENTS = "11111111-0000-0000-0000-000000000001", "22222222-0000-0000-0000-000000000002"
SENSOR = "5e5e5e5e-0000-0000-0000-000000000001"

T_A = FRM + timedelta(days=1, hours=2)                    # Web portal down 30 min
T_B = T_A + timedelta(minutes=25)                         # API gateway down 10 min (overlaps A)
T_E = FRM + timedelta(days=2, hours=5)                    # Payments API down 18 min
FAILED_PROBE = FRM + timedelta(hours=10)                  # DNS: isolated failed probe
GAP = (FRM + timedelta(hours=40), FRM + timedelta(hours=52))   # Intranet: 12 h without probes

OUTAGES = {A: [(T_A, T_A + timedelta(minutes=30))], B: [(T_B, T_B + timedelta(minutes=10))],
           E: [(T_E, T_E + timedelta(minutes=18))]}


def _check(cid, name, ctype="http", group=None, group_name=None, target=None, criticality=None,
           business_owner=None, owner_team=None, tags=(), url=None, enabled=True,
           tls=(None, None, None, None), warn=30, crit=7):
    expiry, days, issuer, subject = tls
    return (cid, name, ctype, "up", enabled, FRM - timedelta(days=60), INTERVAL, group, group_name,
            target, criticality, business_owner, owner_team, list(tags), url, "host.example",
            expiry, days, issuer, subject, warn, crit, TO)


CHECKS = [
    _check(A, "Web portal", group=G_CUSTOMER, group_name="Customer apps", target=99.9,
           criticality="critical", business_owner="Sales", owner_team="Web team", tags=("prod",),
           url="https://portal.example"),
    _check(B, "API gateway", group=G_CUSTOMER, group_name="Customer apps", criticality="high",
           owner_team="Web team", url="https://api.example"),
    _check(C, "DNS resolver", ctype="dns"),
    _check(D, "Legacy FTP", ctype="tcp"),
    _check(E, "Payments API", group=G_PAYMENTS, group_name="Payments", target=99.5,
           criticality="critical", owner_team="Payments team", url="https://pay.example"),
    _check(F, "Intranet", url="http://intranet.example"),
]
NAMES = {r[0]: r[1] for r in CHECKS}


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
                return _CHResult(rows(sql, parameters or {}) if callable(rows) else rows)
        return _CHResult([])

    def sql_with(self, needle: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if needle in c[0]]


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def probes(frm=FRM, to=TO, outages=None):
    """Raw probe rows (cid, ts, is_up, count) every INTERVAL; D never probed,
    C has one failed probe, F has a 12-hour gap, outages fail their probes."""
    outages = OUTAGES if outages is None else outages
    out = []
    for cid in (A, B, C, E, F):
        ts = frm - timedelta(seconds=INTERVAL)
        while ts < to:
            skip = cid == F and GAP[0] <= ts < GAP[1]
            if not skip:
                up = 0 if (cid == C and ts == FAILED_PROBE) else 1
                if any(lo <= ts < hi for lo, hi in outages.get(cid, [])):
                    up = 0
                out.append((cid, ts, up, 1))
            ts += timedelta(seconds=INTERVAL)
    return out


def status_events(outages=None):
    events = [(cid, FRM - timedelta(days=1), "up") for cid in (A, B, C, D, E, F)]
    for cid, spans in (OUTAGES if outages is None else outages).items():
        for lo, hi in spans:
            events += [(cid, lo, "down"), (cid, hi, "up")]
    return events


def ch_routes(raw=None, extra=(), outages=None):
    raw = probes(outages=outages) if raw is None else raw
    events = status_events(outages)

    def raw_rows(sql, p):
        ids, lo, hi = set(p["ids"]), _ts(p["f"]), _ts(p["t"])
        return [r for r in raw if r[0] in ids and lo <= r[1] < hi]

    def prior(sql, p):
        ids, lo = set(p["ids"]), _ts(p["f"])
        latest = {}
        for cid, ts, st in sorted(events, key=lambda e: e[1]):
            if cid in ids and ts < lo:
                latest[cid] = (cid, ts, st)
        return list(latest.values())

    def changes(sql, p):
        ids, lo, hi = set(p["ids"]), _ts(p["f"]), _ts(p["t"])
        return sorted((e for e in events if e[0] in ids and lo <= e[1] < hi), key=lambda e: (e[0], e[1]))

    return [*extra, ("LIMIT 1 BY", prior), ("service_status_log", changes),
            ("min(is_up), count()", raw_rows)]


def db_routes(checks=CHECKS, maintenance=(), vantages=(), sensors=(), policy=99.5):
    return FakeDB([
        ("FROM service_check_maintenance", list(maintenance)),
        ("FROM service_checks sc", list(checks)),
        ("system_settings", [(policy,)] if policy is not None else []),
        ("FROM service_check_vantage_status", list(vantages)),
        ("FROM sensors s", list(sensors)),
    ])


def run(monkeypatch, db, ch, entry, frm=FRM, to=TO, branding=BRAND):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    entries = [entry if isinstance(entry, dict) else {"id": entry}]
    out = asyncio.run(rs.build_sections(db, entries, frm, to, None, requester=None, branding=branding))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        assert all(len(r) == len(t["headers"]) for r in t["rows"])
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)
    return sec


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


def local(dt_naive_utc: datetime) -> datetime:
    return dt_naive_utc.replace(tzinfo=UTC).astimezone(RIYADH)


def day_label(dt_naive_utc: datetime) -> str:
    d = local(dt_naive_utc)
    return f"{d:%b} {d.day:02d}"


# ── registry and templates ──────────────────────────────────────────────────

SECTIONS = ("service_sla_compliance", "service_outages", "service_response_times",
            "service_daily_uptime", "tls_certificate_expiry", "service_by_group",
            "service_maintenance_log", "service_vantage_comparison")
TEMPLATES = ("service_sla_compliance_monthly", "service_incidents", "service_response_times_web",
             "service_daily_uptime_report", "tls_certificate_expiry_report",
             "service_group_owner_availability", "service_maintenance_log_report",
             "service_monitoring_sites")


def test_sections_are_fleet_scoped_and_need_service_checks_view():
    for sid in SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["fn"].__module__ == pack.__name__
        assert e["scope"] == "fleet", sid
        assert e["requires"] == ("service_checks.view",), sid
        assert e["category"] in ("Services", "Security"), sid
    assert rs.SECTION_REGISTRY["tls_certificate_expiry"]["semantics"] == "snapshot"
    assert rs.SECTION_REGISTRY["service_vantage_comparison"]["semantics"] == "snapshot"
    assert rs.SECTION_REGISTRY["service_sla_compliance"]["semantics"] == "window"


def test_templates_are_valid_and_published():
    assert rt.validate_catalog() == []
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category in ("services", "executive", "security"), key
        assert set(t.section_ids) <= set(SECTIONS), key
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
        assert t.requires() == {"service_checks.view"}
    assert rt.TEMPLATES["service_sla_compliance_monthly"].featured
    assert rt.TEMPLATES["service_sla_compliance_monthly"].title == "Service SLA Compliance – Previous Month"
    assert rt.TEMPLATES["service_sla_compliance_monthly"].default_period == "previous_month"


# ── service_sla_compliance ──────────────────────────────────────────────────

def test_sla_compliance_confirmed_basis_verdicts_and_formatting(monkeypatch):
    ch = FakeCH(ch_routes())
    sec = run(monkeypatch, db_routes(), ch, "service_sla_compliance")
    assert [kpi(sec, k)["value"] for k in ("Met", "At risk", "Breached", "Not assessed")] == ["3", "1", "1", "1"]
    assert kpi(sec, "Met")["subtitle"] == "60% of 5 assessed"
    assert kpi(sec, "Breached")["accent"] == "danger"
    assert kpi(sec, "Most error budget used")["value"] == "694.4%"
    assert kpi(sec, "Most error budget used")["subtitle"] == "Web portal"
    covered = 4 * SPAN + (SPAN - (12 * 3600 - INTERVAL))
    fleet = (covered - (1800 + 600 + 1080)) / covered * 100
    assert kpi(sec, "Confirmed uptime")["value"] == f"{int(fleet * 100) / 100:.2f}%"

    donut = sec["charts"][0]["series"]
    assert donut["kind"] == "donut"
    assert {p["label"]: p["value"] for p in donut["points"]} == {
        "Met": 3, "At risk": 1, "Breached": 1, "Not assessed": 1}

    table = sec["tables"][0]
    assert table["styles"][1] == "status" and table["styles"][-1] == "status"
    rows = {r[0]: r for r in table["rows"]}
    assert [r[0] for r in table["rows"]] == ["Web portal", "Payments API", "API gateway",
                                             "DNS resolver", "Intranet", "Legacy FTP"]
    # 30 min down of 3 days = 99.3055…% -> truncated, never rounded up.
    assert rows["Web portal"] == ["Web portal", "critical", "Web team (Sales)", "99.9% (service)",
                                  "99.30%", "100.0%", "30 min", "694.4%", "non-compliant"]
    assert rows["Payments API"] == ["Payments API", "critical", "Payments team", "99.5% (service)",
                                    "99.58%", "100.0%", "18 min", "83.3%", "at risk"]
    assert rows["API gateway"][3:] == ["99.5% (policy)", "99.76%", "100.0%", "10 min", "46.3%", "compliant"]
    # An isolated failed probe is diagnostic evidence, not downtime.
    assert rows["DNS resolver"][1:] == ["—", "—", "99.5% (policy)", "100.00%", "100.0%", "0 s", "0.0%",
                                        "compliant"]
    # A monitoring gap is coverage, never down.
    assert rows["Intranet"][4:7] == ["100.00%", "83.4%", "0 s"]
    # No evidence at all: not assessed, never 0 % or 100 %.
    assert rows["Legacy FTP"][4:] == ["—", "0.0%", "—", "—", "not assessed"]
    assert any("Confirmed availability" in n for n in sec["notes"])
    assert any("not assessed" in n for n in sec["notes"])
    # The evidence is the confirmed basis: probes and the status log were both read.
    assert ch.sql_with("service_status_log") and ch.sql_with("min(is_up), count()")


def test_sla_compliance_attention_and_top_n(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()),
              {"id": "service_sla_compliance", "options": {"show": "attention"}})
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["Web portal", "Payments API"]
    assert sec["tables"][0]["title"].startswith("Services that breached")

    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()),
              {"id": "service_sla_compliance", "options": {"top_n": 2}})
    assert len(sec["tables"][0]["rows"]) == 2
    assert sec["tables"][0]["title"] == "Service SLA compliance (top 2 of 6)"
    # KPIs still count every service.
    assert kpi(sec, "Met")["value"] == "3"


def test_sla_policy_fallback_and_empty_states(monkeypatch):
    sec = run(monkeypatch, db_routes(policy=None), FakeCH(ch_routes()), "service_sla_compliance")
    rows = {r[0]: r for r in sec["tables"][0]["rows"]}
    assert rows["API gateway"][3] == "99.9% (default)"

    sec = run(monkeypatch, db_routes(checks=[]), FakeCH(ch_routes()), "service_sla_compliance")
    assert sec["tables"] == [] and sec["notes"] == ["No service checks existed in this period."]

    ok_only = [c for c in CHECKS if c[0] in (B, C)]
    sec = run(monkeypatch, db_routes(checks=ok_only), FakeCH(ch_routes()),
              {"id": "service_sla_compliance", "options": {"show": "attention"}})
    assert sec["tables"] == []
    assert sec["notes"][0].startswith("No service breached")


def test_maintenance_is_excluded_from_availability_and_incidents(monkeypatch):
    window = ("m1", "check", A, None, None, T_A - timedelta(minutes=10), T_A + timedelta(minutes=40),
              None, None, "Patch", "Web portal", None)
    db = db_routes(maintenance=[window])
    sec = run(monkeypatch, db, FakeCH(ch_routes()), "service_sla_compliance")
    row = next(r for r in sec["tables"][0]["rows"] if r[0] == "Web portal")
    assert row[4:7] == ["100.00%", "100.0%", "0 s"] and row[-1] == "compliant"

    sec = run(monkeypatch, db_routes(maintenance=[window]), FakeCH(ch_routes()), "service_outages")
    assert "Web portal" not in [r[0] for r in sec["tables"][0]["rows"]]
    assert kpi(sec, "Incidents")["value"] == "2"


# ── service_outages ─────────────────────────────────────────────────────────

def test_outages_rows_kpis_per_day_bars_in_org_timezone(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()), "service_outages")
    assert kpi(sec, "Incidents")["value"] == "3"
    assert kpi(sec, "Incidents")["subtitle"] == "3 service(s) affected"
    assert kpi(sec, "Ongoing")["value"] == "0"
    assert kpi(sec, "Confirmed downtime")["value"] == "58 min"
    assert kpi(sec, "Longest incident")["value"] == "30 min"
    assert kpi(sec, "Longest incident")["subtitle"] == "Web portal"
    assert kpi(sec, "Mean time to recover")["value"] == "19 min"          # (30 + 10 + 18) / 3

    table = sec["tables"][0]
    assert table["headers"] == ["Service", "Group", "Started", "Ended", "Duration"]
    assert table["rows"][0] == ["Payments API", "Payments", BRAND.fmt_short(T_E.replace(tzinfo=UTC)),
                                BRAND.fmt_short((T_E + timedelta(minutes=18)).replace(tzinfo=UTC)), "18 min"]
    web = next(r for r in table["rows"] if r[0] == "Web portal")
    # Riyadh is UTC+3: the start reads in organisation time.
    assert web[2] == local(T_A).strftime("%b %d %H:%M")
    assert web[1] == "Customer apps" and web[4] == "30 min"

    bars = sec["charts"][0]["series"]
    assert bars["kind"] == "bars" and len(bars["points"]) == 3
    expected = {}
    for start in (T_A, T_B, T_E):
        midnight = local(start).replace(hour=0, minute=0)
        key = midnight.astimezone(UTC).isoformat().replace("+00:00", "Z")
        expected[key] = expected.get(key, 0) + 1
    assert {p["t"]: p["v"] for p in bars["points"] if p["v"]} == expected
    assert bars["points"][0]["t"] == FRM.isoformat() + "Z"     # local midnight, not UTC midnight


def test_outages_sort_min_minutes_and_top_n(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()),
              {"id": "service_outages", "options": {"sort": "longest", "min_minutes": 15}})
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["Web portal", "Payments API"]
    assert sec["tables"][0]["title"] == "Longest incidents"
    assert any("shorter than 15 min" in n for n in sec["notes"])
    assert kpi(sec, "Incidents")["value"] == "3"

    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()), {"id": "service_outages", "options": {"top_n": 1}})
    assert len(sec["tables"][0]["rows"]) == 1
    assert sec["tables"][0]["title"] == "Most recent incidents (top 1 of 3)"


def test_outage_reaching_the_period_end_is_not_closed(monkeypatch):
    outages = {A: [(TO - timedelta(hours=1), TO + timedelta(hours=2))]}
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes(outages=outages)), "service_outages")
    row = sec["tables"][0]["rows"][0]
    assert row[0] == "Web portal" and row[3] == "After period end" and row[4] == "≥ 1 h 00 min"
    assert kpi(sec, "Mean time to recover")["value"] == "—"


def test_outages_empty_states(monkeypatch):
    sec = run(monkeypatch, db_routes(checks=[]), FakeCH(ch_routes()), "service_outages")
    assert sec["notes"] == ["No service checks existed in this period."]
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes(outages={})), "service_outages")
    assert sec["tables"] == [] and kpi(sec, "Incidents")["value"] == "0"
    assert sec["notes"][0] == "No confirmed service incidents in this period."


# ── service_response_times ──────────────────────────────────────────────────

RT_ROWS = [(A, 1000, [120.0, 451.2, 980.0], 150.2, 1500.0),
           (B, 800, [80.0, 1200.0, 2500.0], 300.0, 3100.0),
           (C, 900, [0.4, 0.9, 1.5], 0.5, 2.0),
           ("99999999-0000-0000-0000-000000000009", 50, [1.0, 2.0, 3.0], 1.0, 4.0)]   # deleted check


def _rt_rows(sql, p):
    ids = set(p.get("ids") or [r[0] for r in RT_ROWS])
    return [r for r in RT_ROWS if r[0] in ids]


def test_response_times_percentiles_in_clickhouse_and_formatting(monkeypatch):
    ch = FakeCH([("quantilesExactIf", _rt_rows)])
    sec = run(monkeypatch, db_routes(), ch, "service_response_times")
    (sql, params), = ch.sql_with("quantilesExactIf")
    assert "is_up = 1" in sql and "GROUP BY sid" in sql and "FROM zenplus.service_metrics " in sql
    assert "service_check_id IN" not in sql
    assert sec["title"] == "Top 25 Services by 95th-Percentile Response Time"
    table = sec["tables"][0]
    assert table["headers"] == ["Service", "Type", "P50", "P95", "P99", "Average", "Max", "Probes"]
    assert table["rows"] == [
        ["API gateway", "HTTP", "80.0 ms", "1.20 s", "2.50 s", "300 ms", "3.10 s", "800"],
        ["Web portal", "HTTP", "120 ms", "451 ms", "980 ms", "150 ms", "1.50 s", "1,000"],
        ["DNS resolver", "DNS", "0.40 ms", "0.90 ms", "1.50 ms", "0.50 ms", "2.00 ms", "900"],
    ]
    assert kpi(sec, "Services measured")["value"] == "3"
    assert kpi(sec, "Slowest P95")["value"] == "1.20 s"
    assert kpi(sec, "Slowest P95")["subtitle"] == "API gateway"
    assert kpi(sec, "Median service P95")["value"] == "451 ms"
    assert kpi(sec, "P95 over 1000 ms")["value"] == "1"
    rank = sec["charts"][0]["series"]
    assert rank["kind"] == "rank" and rank["unit"] == "ms"
    assert [p["label"] for p in rank["points"]] == ["API gateway", "Web portal", "DNS resolver"]


def test_response_times_options(monkeypatch):
    ch = FakeCH([("quantilesExactIf", _rt_rows)])
    sec = run(monkeypatch, db_routes(), ch,
              {"id": "service_response_times", "options": {"check_type": "http", "top_n": 1}})
    (sql, params), = ch.sql_with("quantilesExactIf")
    assert "service_check_id IN %(ids)s" in sql
    assert params["ids"] == sorted([A, B, E, F])
    assert sec["title"] == "Top 1 HTTP Services by 95th-Percentile Response Time"
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["API gateway"]
    assert sec["tables"][0]["title"] == "Response times (top 1 of 2)"

    sec = run(monkeypatch, db_routes(), FakeCH([("quantilesExactIf", _rt_rows)]),
              {"id": "service_response_times", "options": {"sort": "p50"}})
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["Web portal", "API gateway", "DNS resolver"]
    assert sec["title"] == "Top 25 Services by Median Response Time"

    # No check of the requested type: no query, an empty-state note.
    ch = FakeCH([("quantilesExactIf", _rt_rows)])
    sec = run(monkeypatch, db_routes(), ch, {"id": "service_response_times", "options": {"check_type": "tls"}})
    assert not ch.sql_with("quantilesExactIf")
    assert sec["notes"][0] == "No successful TLS service probes were recorded in this period."


def test_response_times_use_rollups_beyond_raw_retention(monkeypatch):
    rows = [(A, 2000, 140.0, 410.0, 1900.0), (B, 1500, 210.0, 650.0, 2500.0)]
    ch = FakeCH([("quantileExactWeighted", rows)])
    frm = TO - timedelta(days=45)
    sec = run(monkeypatch, db_routes(), ch, {"id": "service_response_times", "options": {"sort": "p99"}},
              frm=frm, to=frm + timedelta(days=7))
    (sql, _p), = ch.sql_with("quantileExactWeighted")
    assert "FROM zenplus.service_metrics_5m" in sql and "uptime_pct >= 1" in sql
    assert not ch.sql_with("quantilesExactIf")
    assert sec["tables"][0]["headers"] == ["Service", "Type", "Average", "P95 of 5-min averages", "Max",
                                           "Probes"]
    assert sec["tables"][0]["rows"][0] == ["API gateway", "HTTP", "210 ms", "650 ms", "2.50 s", "1,500"]
    assert sec["title"] == "Top 25 Services by 95th-Percentile Response Time"
    assert any("5-minute rollups" in n for n in sec["notes"])
    assert any("instead of 99th-percentile" in n for n in sec["notes"])


# ── service_daily_uptime ────────────────────────────────────────────────────

def test_daily_uptime_matrix_chart_and_org_timezone(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()), "service_daily_uptime")
    days = [FRM + timedelta(days=i) for i in range(3)]
    table = sec["tables"][0]
    assert table["headers"] == ["Service"] + [day_label(d) for d in days] + ["Period"]
    rows = {r[0]: r for r in table["rows"]}
    a_day = (local(T_A).date() - local(FRM).date()).days
    expected = ["100.00%"] * 3
    expected[a_day] = "97.91%"                      # 30 min down of a local day, truncated
    assert rows["Web portal"][1:4] == expected and rows["Web portal"][4] == "99.30%"
    assert rows["Legacy FTP"][1:] == ["—", "—", "—", "—"]
    assert table["rows"][0][0] == "Web portal"     # lowest uptime first
    assert table["rows"][-1][0] == "Legacy FTP"

    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "bars" and chart["unit"] == "%"
    assert [p["t"] for p in chart["points"]] == [d.isoformat() + "Z" for d in days]
    assert chart["y_domain"][1] == 100
    assert kpi(sec, "Worst day")["subtitle"] == BRAND.fmt_date(days[a_day].replace(tzinfo=UTC))
    assert kpi(sec, "Days with downtime")["value"] == f"{len({a_day, (local(T_E).date() - local(FRM).date()).days})} of 3"
    assert kpi(sec, "Services with downtime")["value"] == "3"
    assert any("Asia/Riyadh" in n for n in sec["notes"])


def test_daily_uptime_downtime_lands_on_the_local_day(monkeypatch):
    """An outage at 22:30 UTC belongs to the next Riyadh calendar day."""
    late = FRM + timedelta(days=1, hours=1, minutes=30)        # 22:30 UTC = 01:30 Riyadh
    outages = {A: [(late, late + timedelta(minutes=60))]}
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes(outages=outages)), "service_daily_uptime")
    assert late.hour == 22 and local(late).hour == 1
    row = next(r for r in sec["tables"][0]["rows"] if r[0] == "Web portal")
    idx = (local(late).date() - local(FRM).date()).days
    assert idx == 1 and late.date() == FRM.date() + timedelta(days=1)
    assert row[1 + idx] == "95.83%"
    assert [c for i, c in enumerate(row[1:4]) if i != idx] == ["100.00%", "100.00%"]


def test_daily_uptime_wide_periods_split_and_weekly_columns(monkeypatch):
    frm = TO - timedelta(days=20)
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes(raw=[])),
              {"id": "service_daily_uptime", "options": {"columns": "days"}}, frm=frm)
    assert [len(t["headers"]) for t in sec["tables"]] == [16 + 2, 4 + 2]
    assert sec["tables"][0]["title"].startswith("Confirmed uptime by day (")
    assert all(c == "—" for t in sec["tables"] for r in t["rows"] for c in r[1:])
    assert sec["charts"] == []                       # nothing measured: no chart of zeros
    assert kpi(sec, "Period uptime")["value"] == "—"
    assert kpi(sec, "Days with downtime")["value"] == "—"
    assert sec["notes"][0] == "No service has confirmed evidence in this period (not measured)."

    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes(raw=[])),
              {"id": "service_daily_uptime", "options": {"columns": "weeks"}}, frm=frm,
              branding=Branding(timezone="Asia/Riyadh", week_start="sunday"))
    weeks = sec["tables"][0]["headers"][1:-1]
    assert 3 <= len(weeks) <= 4 and len(sec["tables"]) == 1
    first_days = [local(frm + timedelta(days=i)) for i in range(20)]
    assert sum(1 for d in first_days if d.weekday() == 6) in (len(weeks) - 1, len(weeks))
    assert any("calendar weeks" in n for n in sec["notes"])


def test_daily_uptime_top_n_and_sort_by_name(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()),
              {"id": "service_daily_uptime", "options": {"sort": "name", "top_n": 2}})
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["API gateway", "DNS resolver"]
    assert sec["tables"][0]["title"] == "Confirmed uptime by day (top 2 of 6)"


# ── tls_certificate_expiry ──────────────────────────────────────────────────

def _tls_fixture():
    now = datetime.now(UTC)
    t1, t2, t3, t4, t5, t6, t7, t8, t9 = (f"7{i}000000-0000-0000-0000-000000000000" for i in range(1, 10))
    checks = [
        _check(t1, "LDAPS", ctype="tls"),
        _check(t2, "Shop", url="https://shop.example"),
        _check(t3, "Old VPN portal", ctype="tls"),
        _check(t4, "Mail TLS", ctype="tls"),
        _check(t5, "New site", url="https://new.example"),
        _check(t6, "SSH", ctype="tcp"),
        _check(t7, "Partner API", ctype="tls", warn=60, crit=45),
        _check(t8, "Paused shop", url="https://paused.example", enabled=False),
        _check(t9, "Billing", ctype="tls", tls=(now + timedelta(days=10, hours=1), 10, "R3", "CN=billing")),
    ]

    def vantage(cid, poller, expiry, subject="CN=x", issuer="R3", valid=True, sensor=None, site=None):
        return (cid, poller, sensor, site, None, "online", "up", now, now, 50.0, None,
                expiry, None, valid, issuer, subject)

    vantages = [
        vantage(t1, "central", now + timedelta(days=3, hours=1), "CN=ldap.example"),
        vantage(t2, "central", now + timedelta(days=20, hours=1), "CN=shop.example", "Let's Encrypt R3"),
        vantage(t2, SENSOR, now + timedelta(days=20, hours=1), "CN=shop.example", "Let's Encrypt R3",
                sensor="branch-sensor", site="Jeddah branch"),
        vantage(t3, "central", now - timedelta(days=2) + timedelta(hours=1), "CN=vpn.example", valid=False),
        vantage(t4, "central", now + timedelta(days=200, hours=1), "CN=mail.example"),
        vantage(t7, "central", now + timedelta(days=50, hours=1), "CN=partner.example"),
    ]
    return checks, vantages


def test_tls_expiry_grades_with_each_checks_thresholds(monkeypatch):
    checks, vantages = _tls_fixture()
    sec = run(monkeypatch, db_routes(checks=checks, vantages=vantages), FakeCH([]), "tls_certificate_expiry")
    assert sec["title"] == "TLS Certificates Expiring Within 90 Days"
    assert [kpi(sec, k)["value"] for k in ("Expired", "Critical", "Warning")] == ["1", "1", "3"]
    assert kpi(sec, "Expiring within 90 days")["value"] == "4"
    assert kpi(sec, "Expiring within 90 days")["subtitle"] == "of 6 certificate(s) seen"
    assert kpi(sec, "No TLS evidence")["value"] == "1"
    assert kpi(sec, "No TLS evidence")["subtitle"] == "of 7 TLS/HTTPS check(s)"
    rows = sec["tables"][0]["rows"]
    assert [(r[0], r[4], r[5]) for r in rows] == [
        ("Old VPN portal", "-2", "expired"), ("LDAPS", "3", "critical"), ("Billing", "10", "warning"),
        ("Shop", "20", "warning"), ("Partner API", "50", "warning")]    # 50 days ≤ its own 60-day warning
    shop = rows[3]
    assert shop[1:3] == ["CN=shop.example", "Let's Encrypt R3"]
    assert shop[6] == "Controller, Jeddah branch · branch-sensor"     # one certificate, two sites
    expiry = vantages[1][11]
    assert shop[3] == BRAND.fmt_date(expiry)
    assert rows[2][6] == "Latest result"
    assert sec["tables"][0]["styles"][5] == "status"
    notes = " ".join(sec["notes"])
    assert "New site" in notes and "validation failed" in notes and "Old VPN portal" in notes
    assert "1 paused TLS/HTTPS check(s)" in notes and "F5" in notes


def test_tls_expiry_horizon_and_top_n(monkeypatch):
    checks, vantages = _tls_fixture()
    sec = run(monkeypatch, db_routes(checks=checks, vantages=vantages), FakeCH([]),
              {"id": "tls_certificate_expiry", "options": {"days": 250}})
    assert sec["title"] == "TLS Certificates Expiring Within 250 Days"
    assert sec["tables"][0]["rows"][-1][0] == "Mail TLS" and sec["tables"][0]["rows"][-1][5] == "ok"

    sec = run(monkeypatch, db_routes(checks=checks, vantages=vantages), FakeCH([]),
              {"id": "tls_certificate_expiry", "options": {"top_n": 2}})
    assert len(sec["tables"][0]["rows"]) == 2
    assert sec["tables"][0]["title"] == "Certificates needing attention (top 2 of 5)"

    sec = run(monkeypatch, db_routes(checks=[c for c in checks if c[2] == "tcp"]), FakeCH([]),
              "tls_certificate_expiry")
    assert sec["notes"] == ["No enabled TLS or HTTPS service checks are configured."]


# ── service_by_group ────────────────────────────────────────────────────────

def test_by_group_merged_downtime_and_impact(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()), "service_by_group")
    assert sec["title"] == "Availability by Service Group and Owner"
    assert kpi(sec, "Merged downtime")["value"] == "53 min"            # A ∪ B = 35 min, + E 18 min
    assert kpi(sec, "Impact")["value"] == "58.0 service-min"           # 30 + 10 + 18
    assert kpi(sec, "Service groups")["value"] == "3"
    assert kpi(sec, "Ungrouped services")["value"] == "3"
    by_group, by_team = sec["tables"]
    assert by_group["headers"][0] == "Service group" and by_team["headers"][0] == "Owner team"
    rows = {r[0]: r for r in by_group["rows"]}
    assert rows["Customer apps"][1:] == ["2", "2", "99.53%", "2", "35 min", "40.0", "Web portal (99.30%)"]
    assert rows["Payments"][5:7] == ["18 min", "18.0"]
    assert rows["Ungrouped"][1:3] == ["3", "2"] and rows["Ungrouped"][5] == "0 s"
    assert [r[0] for r in by_group["rows"]] == ["Customer apps", "Payments", "Ungrouped"]
    assert [r[0] for r in by_team["rows"]] == ["Web team", "Payments team", "No owner team"]
    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "rank" and chart["points"][0] == {"label": "Customer apps", "value": 35.0}


def test_by_group_options(monkeypatch):
    sec = run(monkeypatch, db_routes(), FakeCH(ch_routes()),
              {"id": "service_by_group", "options": {"group_by": "criticality", "top_n": 2}})
    assert sec["title"] == "Availability by Criticality"
    assert len(sec["tables"]) == 1
    assert kpi(sec, "Criticality levels")["value"] == "3"
    assert kpi(sec, "Criticality not set")["value"] == "3"
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["critical", "high"]
    assert sec["tables"][0]["title"] == "By criticality (top 2 of 3)"


# ── service_maintenance_log ─────────────────────────────────────────────────

def _windows():
    now = datetime.now(UTC)
    frm = FRM.replace(tzinfo=UTC)
    to = TO.replace(tzinfo=UTC)
    return [
        ("w1", "group", None, G_CUSTOMER, None, frm + timedelta(hours=6), frm + timedelta(hours=8),
         None, None, "Firmware upgrade", None, "Customer apps"),
        ("w2", "check", A, None, None, frm + timedelta(hours=30), frm + timedelta(hours=34),
         frm + timedelta(hours=31), None, "Certificate swap", "Web portal", None),
        ("w3", "tag", None, None, "prod", frm + timedelta(hours=50), frm + timedelta(hours=52),
         None, frm + timedelta(hours=40), "Postponed", None, None),
        ("w4", "all", None, None, None, to - timedelta(hours=1), now + timedelta(hours=2),
         None, None, None, None, None),
    ]


def test_maintenance_log_states_scope_and_excluded_time(monkeypatch):
    db = db_routes(maintenance=_windows())
    sec = run(monkeypatch, db, FakeCH([]), "service_maintenance_log")
    assert kpi(sec, "Windows")["value"] == "4"
    assert kpi(sec, "Windows")["subtitle"] == "3 applied · 1 active now"
    assert kpi(sec, "Time in maintenance")["value"] == "4 h 00 min"
    assert kpi(sec, "Monitors covered")["value"] == "6"
    assert kpi(sec, "Monitors covered")["subtitle"] == "11.0 monitor-hours excluded from SLA"
    assert kpi(sec, "Ended early")["value"] == "1" and kpi(sec, "Cancelled")["value"] == "1"
    rows = sec["tables"][0]["rows"]
    assert [(r[0], r[1], r[6]) for r in rows] == [
        ("Group: Customer apps", "2", "completed"), ("Monitor: Web portal", "1", "ended early"),
        ("Tag: prod", "1", "cancelled"), ("All monitors", "6", "active")]
    w2 = rows[1]
    assert w2[2] == BRAND.fmt_short(_windows()[1][5]) and w2[4] == BRAND.fmt_short(_windows()[1][7])
    assert w2[5] == "1 h 00 min" and w2[7] == "Certificate swap"
    assert rows[2][4] == "—" and rows[2][5] == "—"            # cancelled: never applied
    assert rows[3][5] == "1 h 00 min" and rows[3][7] == "—"   # clamped to the period end
    (sql, params), = [c for c in db.calls if "FROM service_check_maintenance" in c[0]]
    assert params["f"] == FRM.replace(tzinfo=UTC)


def test_maintenance_log_options_and_empty(monkeypatch):
    sec = run(monkeypatch, db_routes(maintenance=_windows()), FakeCH([]),
              {"id": "service_maintenance_log", "options": {"include_cancelled": False, "top_n": 2}})
    assert kpi(sec, "Windows")["value"] == "3"
    assert [r[6] for r in sec["tables"][0]["rows"]] == ["completed", "ended early"]
    assert sec["tables"][0]["title"] == "Maintenance windows (top 2 of 3)"

    sec = run(monkeypatch, db_routes(), FakeCH([]), "service_maintenance_log")
    assert sec["notes"] == ["No service maintenance windows in this period."]


# ── service_vantage_comparison ──────────────────────────────────────────────

def _vantage_fixture():
    now = datetime.now(UTC)

    def v(cid, poller, state, age_s, latency, error=None):
        sensor = (None, None, None, None) if poller == "central" else \
            ("branch-sensor", "Jeddah branch", "Jeddah", "online")
        return (cid, poller, *sensor, state, now - timedelta(hours=2), now - timedelta(seconds=age_s),
                latency, error, None, None, None, None, None)

    vantages = [v(A, "central", "up", 30, 120.0), v(A, SENSOR, "down", 30, None, "connection timed out"),
                v(B, "central", "up", 30, 80.0), v(B, SENSOR, "up", 3600, 95.0),
                v(C, "central", "up", 30, 0.5)]
    period = [("central", 3000, 2990, 150.0), (SENSOR, 1000, 900, 310.0), ("old-poller-x", 50, 50, 20.0)]
    sensors = [(SENSOR, "branch-sensor", "Jeddah branch", "Jeddah", "online")]
    return vantages, period, sensors


def test_vantage_comparison_sites_disagreement_and_stale(monkeypatch):
    monkeypatch.delenv("POLLER_ID", raising=False)
    vantages, period, sensors = _vantage_fixture()
    ch = FakeCH([("AS source", period)])
    sec = run(monkeypatch, db_routes(vantages=vantages, sensors=sensors), ch, "service_vantage_comparison")
    (sql, params), = ch.sql_with("AS source")
    assert params["controller"] == "poller-01" and "quantileTDigestIf(0.95)" in sql
    assert kpi(sec, "Monitoring sites")["value"] == "3"
    assert kpi(sec, "Monitors on 2+ sites")["value"] == "2"
    assert kpi(sec, "Sites disagree")["value"] == "1"
    assert kpi(sec, "Stale site results")["value"] == "1"
    sites = sec["tables"][0]["rows"]
    assert sites[0] == ["Controller", "online", "3", "3", "0", "0", "0", "0", "80.0 ms", "150 ms", "99.7%"]
    assert sites[1] == ["Jeddah branch · branch-sensor", "online", "2", "0", "1", "0", "0", "1", "—",
                        "310 ms", "90.0%"]
    assert sites[2][0] == "Unknown poller (old-poller-x)" and sites[2][2] == "0" and sites[2][9] == "20.0 ms"
    detail = sec["tables"][1]
    assert detail["title"] == "Monitors whose sites disagree"
    assert [r[:3] for r in detail["rows"]] == [["Web portal", "Controller", "up"],
                                               ["Web portal", "Jeddah branch · branch-sensor", "down"]]
    assert detail["rows"][1][5] == "connection timed out"
    assert [p["label"] for p in sec["charts"][0]["series"]["points"]][:2] == [
        "Controller", "Jeddah branch · branch-sensor"]


def test_vantage_comparison_ignores_paused_monitors(monkeypatch):
    vantages, period, sensors = _vantage_fixture()
    checks = [(*c[:4], False, *c[5:]) if c[0] == B else c for c in CHECKS]     # API gateway paused
    sec = run(monkeypatch, db_routes(checks=checks, vantages=vantages, sensors=sensors),
              FakeCH([("AS source", period)]), "service_vantage_comparison")
    assert kpi(sec, "Stale site results")["value"] == "0"
    assert kpi(sec, "Monitors on 2+ sites")["value"] == "1"
    assert sec["tables"][0]["rows"][0][2] == "2"                  # Controller: Web portal + DNS


def test_vantage_comparison_detail_options_and_empty(monkeypatch):
    vantages, period, sensors = _vantage_fixture()
    sec = run(monkeypatch, db_routes(vantages=vantages, sensors=sensors), FakeCH([("AS source", period)]),
              {"id": "service_vantage_comparison", "options": {"detail": "all"}})
    rows = sec["tables"][1]["rows"]
    assert [(r[0], r[2]) for r in rows] == [("Web portal", "up"), ("Web portal", "down"),
                                            ("API gateway", "up"), ("API gateway", "stale")]

    sec = run(monkeypatch, db_routes(vantages=vantages, sensors=sensors), FakeCH([("AS source", period)]),
              {"id": "service_vantage_comparison", "options": {"detail": "none"}})
    assert len(sec["tables"]) == 1

    sec = run(monkeypatch, db_routes(), FakeCH([]), "service_vantage_comparison")
    assert sec["notes"][0] == "No monitoring-site results are recorded for service checks."


# ── formatting helpers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, "—"), (100.0, "100.00%"), (99.999, "99.99%"), (99.996, "99.99%"), (99.305, "99.30%"),
    (0.0, "0.00%")])
def test_availability_is_two_decimals_truncated(value, expected):
    assert pack._avail(value) == expected


@pytest.mark.parametrize("value,expected", [
    (None, "—"), (0.42, "0.42 ms"), (12.34, "12.3 ms"), (245.2, "245 ms"), (1200.0, "1.20 s")])
def test_response_time_precision(value, expected):
    assert pack._ms(value) == expected
