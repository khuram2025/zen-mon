"""NetPath and Discovery section pack (report_packs/netpath_discovery).

Every section runs through report_sections.build_sections against a fake
Postgres that answers canned rows keyed by SQL substrings (neither module
uses ClickHouse). The window is the seven local days before today in
Asia/Riyadh (UTC+3), so day buckets, chart timestamps and table times all
prove the organisation timezone is honoured."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import netpath_discovery as nd
from tests.conftest import _NullTransaction

RIYADH = ZoneInfo("Asia/Riyadh")
BRAND = Branding(timezone="Asia/Riyadh")
_TODAY = datetime.now(RIYADH).replace(hour=0, minute=0, second=0, microsecond=0)
TO = _TODAY.astimezone(timezone.utc).replace(tzinfo=None)       # local midnight, naive UTC
FRM = TO - timedelta(days=7)
FRM_UTC = FRM.replace(tzinfo=timezone.utc)
# 12:00 UTC is 15:00 in Riyadh.
OCT1_NOON = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

NETPATH_SECTIONS = ("netpath_probe_health", "netpath_path_changes", "netpath_latency_trend",
                    "netpath_problem_hops")
DISCOVERY_SECTIONS = ("discovery_activity", "discovery_unmanaged", "discovery_credential_gaps")
TEMPLATE_KEYS = ("netpath_path_health", "netpath_route_stability", "netpath_latency_report",
                 "netpath_problem_hops_report", "discovery_activity_report", "discovery_unmanaged_devices",
                 "discovery_credential_gaps_report")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


class NoCH:
    def query(self, *a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("NetPath and Discovery sections must not query ClickHouse")


def run(monkeypatch, sid, db, title=None, frm=FRM, to=TO, **options):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: NoCH())

    async def no_dataset(self, name):  # fleet-wide builders are not used by this pack
        raise AssertionError(f"{sid} must not call ctx.dataset({name!r})")
    monkeypatch.setattr(rs.SectionCtx, "dataset", no_dataset)
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

def test_sections_are_fleet_scoped_with_module_permissions():
    for sid in NETPATH_SECTIONS + DISCOVERY_SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["fn"].__module__ == nd.__name__
        assert e["scope"] == "fleet", sid
        assert e["category"] != "Traffic"
    for sid in NETPATH_SECTIONS:
        assert rs.SECTION_REGISTRY[sid]["requires"] == ("netpath.view",)
        assert rs.SECTION_REGISTRY[sid]["category"] == "Paths"
        assert rs.SECTION_REGISTRY[sid]["semantics"] == "window"
    for sid in DISCOVERY_SECTIONS:
        assert rs.SECTION_REGISTRY[sid]["requires"] == ("discovery.view",)
        assert rs.SECTION_REGISTRY[sid]["category"] == "Discovery"
    assert rs.SECTION_REGISTRY["discovery_unmanaged"]["semantics"] == "snapshot"
    assert rs.SECTION_REGISTRY["discovery_credential_gaps"]["semantics"] == "snapshot"
    assert not rs.may_view_section(frozenset({"devices.view"}), "netpath_probe_health")
    assert rs.may_view_section(frozenset({"netpath.view"}), "netpath_probe_health")
    assert not rs.may_view_section(frozenset({"netpath.view"}), "discovery_unmanaged")


def test_templates_are_valid_and_published():
    assert set(TEMPLATE_KEYS) <= set(rt.TEMPLATES)
    assert rt.validate_catalog() == []
    for key in TEMPLATE_KEYS:
        t = rt.TEMPLATES[key]
        assert t.category == ("paths" if key.startswith("netpath") else "inventory"), key
        assert set(t.audience) <= set(rt.AUDIENCES)
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
        assert t.requires() == ({"netpath.view"} if key.startswith("netpath") else {"discovery.view"})
    assert rt.TEMPLATES["netpath_path_health"].title == "Network Path Health"
    assert rt.TEMPLATES["discovery_unmanaged_devices"].featured
    # The latency report charts two different rankings of the same section.
    entries = rt.TEMPLATES["netpath_latency_report"].sections
    assert [e["options"].get("rank") for e in entries[:2]] == ["rtt", "loss"]


# ── netpath_probe_health ────────────────────────────────────────────────────

def _probe(pid, name, *, runs, reached, avg_rtt=None, p95=None, loss=None, worst=None, status="ok",
           enabled=True, prior_runs=0, prior_reached=0, changes=0, prior_changes=0, traced=None,
           paths=1, last_change=None, host="8.8.8.8", protocol="icmp", port=None):
    return {"id": pid, "name": name, "target_host": host, "target_port": port, "protocol": protocol,
            "enabled": enabled, "status": status, "runs": runs, "reached": reached,
            "traced": runs if traced is None else traced, "avg_rtt": avg_rtt, "p95_rtt": p95,
            "avg_loss": loss, "avg_worst_hop": worst, "changes": changes, "last_change": last_change,
            "paths": paths, "prior_runs": prior_runs, "prior_reached": prior_reached,
            "prior_changes": prior_changes}


def _stats():
    return [
        _probe("p1", "Google DNS", runs=2016, reached=2016, avg_rtt=24.4, p95=29.1, loss=0.2, worst=12.5,
               prior_runs=2016, prior_reached=2010, changes=3, prior_changes=5, paths=2,
               last_change=OCT1_NOON),
        _probe("p2", "CRM SaaS", runs=2000, reached=1600, avg_rtt=101.6, p95=180.4, loss=1.5, worst=40.0,
               status="unreached", prior_runs=2000, prior_reached=1990, changes=40, prior_changes=10,
               paths=4, host="crm.example.com", protocol="tcp", port=443),
        # 99,999 of 100,000 must never round up to 100.00%.
        _probe("p3", "Branch VPN", runs=100000, reached=99999, avg_rtt=8.42, p95=9.9, loss=0.0, worst=0.0,
               status="degraded", prior_runs=100000, prior_reached=100000),
        # Enabled but never ran in the window: not measured, never zero.
        _probe("p4", "New probe", runs=0, reached=0, status="pending", traced=0, paths=0),
        # Disabled and silent: left out entirely.
        _probe("p5", "Retired", runs=0, reached=0, status="disabled", enabled=False, traced=0, paths=0),
    ]


def _netpath_db(stats=None, extra=None, trend=None):
    return FakeDB([
        ("percentile_cont(0.95)", stats if stats is not None else _stats()),
        ("top_path_runs", extra or []),
        ("sum(b.changes)", trend or []),
    ])


def test_probe_health_kpis_ranking_and_formatting(monkeypatch):
    db = _netpath_db()
    sec = run(monkeypatch, "netpath_probe_health", db)
    sql, params = db.call("percentile_cont(0.95)")
    assert "FILTER (WHERE s.run_at >= :f AND s.reached) AS avg_rtt" in sql
    assert params["f"] == FRM and params["t"] == TO and params["pf"] == FRM - timedelta(days=7)

    assert kpi(sec, "Path probes")["value"] == "4"           # the disabled, silent probe is left out
    reach = kpi(sec, "Reachability")
    assert reach["value"] == f"{(2016 + 1600 + 99999) / 104016 * 100:.2f}%"
    assert reach["subtitle"].startswith("▼ ") and reach["subtitle"].endswith(" pts vs previous period")
    # Sample-weighted over reached runs.
    expected_rtt = (24.4 * 2016 + 101.6 * 1600 + 8.42 * 99999) / (2016 + 1600 + 99999)
    assert kpi(sec, "Average RTT")["value"] == nd._ms(expected_rtt) == "10 ms"
    assert kpi(sec, "Highest p95 RTT")["value"] == "180 ms"
    assert kpi(sec, "Highest p95 RTT")["subtitle"] == "CRM SaaS"
    assert kpi(sec, "Unhealthy now")["value"] == "2"         # degraded + unreachable
    assert kpi(sec, "Unhealthy now")["accent"] == "danger"

    t = table(sec, "Probes by lowest reachability")
    assert [r[0] for r in t["rows"]] == ["CRM SaaS", "Branch VPN", "Google DNS", "New probe"]
    assert t["headers"] == ["Probe", "Target", "Reachability", "Avg RTT", "p95 RTT", "Avg loss", "Hop loss",
                            "Status"]
    assert t["rows"][0] == ["CRM SaaS", "crm.example.com:443/tcp", "80.00%", "102 ms", "180 ms", "1.50%",
                            "40.00%", "unreachable"]
    assert t["rows"][1][2] == "99.99%"                      # never rounded up to 100.00%
    assert t["rows"][1][3] == "8.4 ms" and t["rows"][1][7] == "degraded"
    assert t["rows"][2][:3] == ["Google DNS", "8.8.8.8", "100.00%"]
    assert t["rows"][3] == ["New probe", "8.8.8.8", "—", "—", "—", "—", "—", "pending"]
    rank = chart(sec, "Runs that did not reach the target")["series"]
    assert rank["kind"] == "rank" and rank["unit"] == "%"
    # Probes that always (or to two decimals always) reached are not charted.
    assert rank["points"] == [{"label": "CRM SaaS", "value": 20.0}]
    assert any("did not run in this period (not measured)" in n for n in sec["notes"])
    assert any("rate-limit" in n for n in sec["notes"])


def test_probe_health_sort_and_top_n(monkeypatch):
    sec = run(monkeypatch, "netpath_probe_health", _netpath_db(), sort="rtt", top_n=2)
    t = table(sec, "Probes by highest p95 latency")
    assert t["title"] == "Probes by highest p95 latency (top 2 of 4)"
    assert [r[0] for r in t["rows"]] == ["CRM SaaS", "Google DNS"]
    rank = chart(sec, "Highest 95th-percentile")["series"]
    assert rank["unit"] == "ms" and [p["label"] for p in rank["points"]] == ["CRM SaaS", "Google DNS",
                                                                              "Branch VPN"]
    sec = run(monkeypatch, "netpath_probe_health", _netpath_db(), sort="loss")
    assert [r[0] for r in table(sec, "Probes by highest packet loss")["rows"]][:2] == ["CRM SaaS",
                                                                                       "Google DNS"]


def test_probe_health_empty_states(monkeypatch):
    sec = run(monkeypatch, "netpath_probe_health", FakeDB())
    assert sec["notes"] == ["No NetPath probes are configured."] and not sec["kpis"]
    idle = [_probe("p4", "New probe", runs=0, reached=0, status="stale", traced=0, paths=0)]
    sec = run(monkeypatch, "netpath_probe_health", _netpath_db(stats=idle))
    assert sec["notes"] == ["No NetPath runs in this period."]


def test_probe_health_unreached_only_is_not_measured(monkeypatch):
    dark = [_probe("p2", "CRM SaaS", runs=10, reached=0, status="unreached", prior_runs=0)]
    sec = run(monkeypatch, "netpath_probe_health", _netpath_db(stats=dark))
    assert kpi(sec, "Reachability")["value"] == "0.00%"
    assert kpi(sec, "Reachability")["subtitle"] == "No runs in the previous period"
    assert kpi(sec, "Average RTT")["value"] == "—"
    assert kpi(sec, "Average loss")["value"] == "—"
    assert kpi(sec, "Highest p95 RTT")["value"] == "—"
    assert table(sec, "Probes")["rows"][0][3:7] == ["—", "—", "—", "—"]


# ── netpath_path_changes ────────────────────────────────────────────────────

def test_path_changes_ranking_trend_and_timezone(monkeypatch):
    db = _netpath_db(
        extra=[{"id": "p1", "top_path_runs": 1900, "new_paths": 1, "outages": 0},
               {"id": "p2", "top_path_runs": 1000, "new_paths": 0, "outages": 3},
               {"id": "p3", "top_path_runs": 100000, "new_paths": 0, "outages": 0}],
        trend=[{"bucket": FRM_UTC + timedelta(days=i), "runs": 0 if i == 4 else 576,
                "changes": {0: 5, 2: 38}.get(i, 0)} for i in range(7)])
    sec = run(monkeypatch, "netpath_path_changes", db)
    sql, params = db.call("sum(b.changes)")
    assert params["org_tz"] == "Asia/Riyadh" and ":org_tz" in sql
    assert db.call("top_path_runs")[1]["ids"] == ["p1", "p2", "p3"]   # probes that ran

    assert kpi(sec, "Path changes")["value"] == "43"
    assert kpi(sec, "Path changes")["subtitle"] == "▲ 28 vs previous period"
    assert kpi(sec, "Probes with changes")["value"] == "2"
    assert kpi(sec, "New routes")["value"] == "1"
    assert kpi(sec, "Reachability lost")["value"] == "3"
    bars = chart(sec, "Path changes per day")["series"]
    assert bars["kind"] == "bars"
    assert [p["t"] for p in bars["points"]] == [_iso(FRM_UTC + timedelta(days=i)) for i in range(7)]
    assert [p["v"] for p in bars["points"]] == [5, 0, 38, 0, None, 0, 0]   # no runs: a gap
    assert bars["points"][0]["t"].endswith("T21:00:00Z")   # local midnight in Riyadh

    t = table(sec, "Route stability by probe")
    assert t["headers"] == ["Probe", "Changes", "Routes", "On primary route", "New routes", "Outages",
                            "Last change"]
    assert [r[0] for r in t["rows"]] == ["CRM SaaS", "Google DNS", "Branch VPN"]
    assert t["rows"][0][1:6] == ["40", "4", "50.00%", "0", "3"]
    assert t["rows"][1][3] == "94.25%"
    assert t["rows"][1][6] == "Oct 01 15:00"                # organisation timezone
    assert t["rows"][0][6] == "—"
    assert any("GMT+3" in n for n in sec["notes"])


def test_path_changes_hourly_buckets_and_top_n(monkeypatch):
    frm = TO - timedelta(hours=24)
    db = _netpath_db(extra=[{"id": "p2", "top_path_runs": 10, "new_paths": 0, "outages": 0}])
    sec = run(monkeypatch, "netpath_path_changes", db, frm=frm, top_n=1)
    bars = chart(sec, "Path changes per hour")["series"]
    assert len(bars["points"]) == 24
    assert table(sec, "Route stability by probe")["title"] == "Route stability by probe (top 1 of 3)"


def test_path_changes_empty(monkeypatch):
    sec = run(monkeypatch, "netpath_path_changes", FakeDB())
    assert sec["notes"] == ["No NetPath probes are configured."]


# ── netpath_latency_trend ───────────────────────────────────────────────────

def _trend_rows(n_probes=3):
    rows = []
    b0 = FRM_UTC                                            # first 2-hour bucket (local 00:00)
    for i in range(n_probes):
        pid, name = f"q{i}", f"Probe {i}"
        for k in range(3):
            if i == 0 and k == 1:
                # A bucket whose runs never reached the target: a gap, not zero.
                rows.append({"id": pid, "name": name, "bucket": b0 + timedelta(hours=2 * k), "runs": 2,
                             "reached": 0, "rtt": None, "loss": None, "jitter": None})
                continue
            rows.append({"id": pid, "name": name, "bucket": b0 + timedelta(hours=2 * k), "runs": 24,
                         "reached": 24, "rtt": 10.0 * (i + 1) + k, "loss": float(n_probes - i) / 2,
                         "jitter": 1.0 + i})
    return rows


def test_latency_trend_series_gaps_and_ranking(monkeypatch):
    db = FakeDB([("avg(s.jitter_ms)", _trend_rows())])
    sec = run(monkeypatch, "netpath_latency_trend", db, top_n=2)
    sql, params = db.call("avg(s.jitter_ms)")
    assert params["org_tz"] == "Asia/Riyadh" and "make_interval" in sql      # 2-hour local grid
    assert sec["title"] == "Path Latency Trend — Top 2 Probes by RTT"
    rtt = chart(sec, "Round-trip time by probe")["series"]
    assert rtt["kind"] == "line" and rtt["unit"] == "ms"
    assert [s["name"] for s in rtt["series"]] == ["All probes (average)", "Probe 2", "Probe 1"]
    assert len(rtt["points"]) == 84                         # 7 days of 2-hour buckets
    assert rtt["points"][0] == {"t": _iso(FRM_UTC), "v": 20.0}
    assert rtt["series"][1]["points"][0] == {"t": _iso(FRM_UTC), "v": 30.0}
    assert rtt["series"][1]["points"][3]["v"] is None       # no runs: a gap
    # The all-probe line leaves out the probe whose runs did not reach the target.
    assert rtt["points"][1]["v"] == 26.0
    loss = chart(sec, "Packet loss by probe")["series"]
    assert loss["unit"] == "%" and loss["series"][1]["points"][0]["v"] == 0.5
    assert kpi(sec, "Peak interval RTT")["value"] == "32 ms"
    assert kpi(sec, "Peak interval RTT")["subtitle"] == f"Probe 2 · {BRAND.fmt_short(FRM_UTC + timedelta(hours=4))}"
    assert kpi(sec, "Probes charted")["subtitle"] == "of 3 with runs"
    t = table(sec, "Charted probes")
    assert t["title"] == "Charted probes (top 2 of 3)"
    assert t["rows"][0] == ["Probe 2", "100.00%", "31 ms", "30 ms", "32 ms", "3.0 ms", "0.50%"]

    sec = run(monkeypatch, "netpath_latency_trend", FakeDB([("avg(s.jitter_ms)", _trend_rows())]),
              top_n=1, rank="loss")
    assert sec["title"] == "Path Latency Trend — Top 1 Probes by Packet Loss"
    assert [s["name"] for s in chart(sec, "Packet loss")["series"]["series"]] == ["All probes (average)",
                                                                                   "Probe 0"]
    sec = run(monkeypatch, "netpath_latency_trend", FakeDB([("avg(s.jitter_ms)", _trend_rows())]),
              top_n=1, rank="reach")
    assert table(sec, "Charted probes")["rows"][0][:2] == ["Probe 0", "96.00%"]   # 48 of 50 runs


def test_latency_trend_splits_many_probes_across_charts(monkeypatch):
    sec = run(monkeypatch, "netpath_latency_trend", FakeDB([("avg(s.jitter_ms)", _trend_rows(7))]),
              top_n=7)
    titles = [c["title"] for c in sec["charts"]]
    assert titles == ["Round-trip time by probe (1–5 of 7)", "Round-trip time by probe (6–7 of 7)",
                      "Packet loss by probe (1–5 of 7)", "Packet loss by probe (6–7 of 7)"]
    for c in sec["charts"]:
        assert len(c["series"]["series"]) <= nd.PROBES_PER_CHART + 1
    assert len(table(sec, "Charted probes")["rows"]) == 7
    # The option is clamped to what the charts can show.
    assert rs.normalize_options("netpath_latency_trend", {"top_n": 50})["top_n"] == 10


def test_latency_trend_empty_states(monkeypatch):
    sec = run(monkeypatch, "netpath_latency_trend", FakeDB())
    assert sec["notes"] == ["No NetPath runs in this period."]
    dark = [{"id": "q", "name": "Dark", "bucket": FRM_UTC, "runs": 3, "reached": 0, "rtt": None,
             "loss": None, "jitter": None}]
    sec = run(monkeypatch, "netpath_latency_trend", FakeDB([("avg(s.jitter_ms)", dark)]))
    assert sec["notes"] == ["No NetPath run reached its target in this period."]


# ── netpath_problem_hops ────────────────────────────────────────────────────

def _hops_db(probes=None, hops=None, meta=None):
    return FakeDB([
        ("x.lossy, x.carried", probes if probes is not None else [
            {"id": "p1", "name": "Google DNS", "reached": 2000, "lossy": 1200, "carried": 120},
            {"id": "p2", "name": "CRM SaaS", "reached": 1000, "lossy": 600, "carried": 420},
        ]),
        ("WITH picked AS", hops if hops is not None else [
            {"ip": "62.149.1.1", "lossy_runs": 1100.0, "avg_loss": 66.67, "peak_loss": 100.0, "carried": 0.05,
             "probe_ids": ["p1", "p2"], "total_hops": 4},
            {"ip": "185.10.20.30", "lossy_runs": 500.0, "avg_loss": 33.33, "peak_loss": 50.0, "carried": 0.7,
             "probe_ids": ["p2"], "total_hops": 4},
            {"ip": "10.0.0.1", "lossy_runs": 40.0, "avg_loss": 25.0, "peak_loss": 25.0, "carried": 0.3,
             "probe_ids": ["p1"], "total_hops": 4},
        ]),
        ("FROM netpath_hop_meta", meta if meta is not None else [
            {"ip": "62.149.1.1", "hostname": "edge1.isp.example", "asn": 64500, "as_name": "Example ISP",
             "country": "SA", "is_internal": False, "enriched_at": OCT1_NOON, "device_name": None},
            {"ip": "185.10.20.30", "hostname": None, "asn": None, "as_name": None, "country": None,
             "is_internal": False, "enriched_at": None, "device_name": None},
            {"ip": "10.0.0.1", "hostname": "core-gw", "asn": None, "as_name": None, "country": None,
             "is_internal": True, "enriched_at": OCT1_NOON, "device_name": "core-gw-01"},
        ]),
    ])


def test_problem_hops_owners_assessment_and_shares(monkeypatch):
    db = _hops_db()
    sec = run(monkeypatch, "netpath_problem_hops", db, top_n=3, min_loss=20)
    params = db.call("WITH picked AS")[1]
    assert params["thr"] == 20.0 and params["lim"] == 3 and params["cap"] == nd.HOP_RUN_CAP
    assert db.call("FROM netpath_hop_meta")[1]["ips"] == ["62.149.1.1", "185.10.20.30", "10.0.0.1"]
    assert sec["title"] == "Top 3 Problem Hops by Packet Loss"
    assert kpi(sec, "Hops with loss")["value"] == "4"
    assert kpi(sec, "Runs with hop loss")["value"] == "60.00%"
    assert kpi(sec, "Runs with hop loss")["subtitle"] == "1,800 of 3,000 runs had a hop at ≥ 20%"
    assert kpi(sec, "Target also lost packets")["value"] == "30.00%"
    assert kpi(sec, "Internal / external")["value"] == "1 / 2"
    t = table(sec, "Hops with loss ≥ 20%")
    assert t["title"] == "Hops with loss ≥ 20% (top 3 of 4)"
    assert t["headers"] == ["Hop", "Network", "Probes", "% of runs", "Avg loss", "Target lost", "Likely cause"]
    assert t["rows"][0] == ["62.149.1.1", "AS64500 Example ISP · edge1.isp.example", "2", "36.67%", "66.67%",
                            "5.00%", "Rate limiting"]
    assert t["rows"][1] == ["185.10.20.30", "Lookup pending", "1", "50.00%", "33.33%", "70.00%",
                            "Loss to target"]
    assert t["rows"][2][:3] == ["10.0.0.1", "Managed device core-gw-01 · core-gw", "1"]
    assert t["rows"][2][6] == "Mixed"
    assert chart(sec, "Runs with loss by hop")["series"]["points"][0] == {"label": "edge1.isp.example",
                                                                          "value": 1100}
    assert sec["notes"][0] == nd._ICMP_CAVEAT
    assert not any("sample" in n for n in sec["notes"])


def test_problem_hops_marks_sampled_estimates(monkeypatch):
    probes = [{"id": "p1", "name": "Busy", "reached": 9000, "lossy": nd.HOP_RUN_CAP + 1, "carried": 10}]
    hops = [{"ip": "62.149.1.1", "lossy_runs": 2600.4, "avg_loss": 50.0, "peak_loss": 50.0, "carried": 0.0,
             "probe_ids": ["p1"], "total_hops": 1}]
    sec = run(monkeypatch, "netpath_problem_hops", _hops_db(probes=probes, hops=hops, meta=[]))
    row = table(sec, "Hops with loss")["rows"][0]
    assert row[3] == "≈ 28.89%" and row[1] == "Lookup pending"
    assert chart(sec, "Runs with loss by hop")["series"]["points"] == [{"label": "62.149.1.1", "value": 2600}]
    assert any("even sample" in n for n in sec["notes"])


def test_problem_hops_empty_states(monkeypatch):
    sec = run(monkeypatch, "netpath_problem_hops", _hops_db(probes=[]))
    assert sec["notes"] == ["No NetPath run reached its target in this period."]
    clean = [{"id": "p1", "name": "Google DNS", "reached": 500, "lossy": 0, "carried": 0}]
    db = _hops_db(probes=clean)
    sec = run(monkeypatch, "netpath_problem_hops", db)
    assert not any("WITH picked AS" in c[0] for c in db.calls)   # no JSON expansion needed
    assert kpi(sec, "Hops with loss")["value"] == "0"
    assert kpi(sec, "Target also lost packets")["value"] == "—"
    assert sec["notes"][0] == "No intermediate hop dropped 10% or more of its probes in this period."
    assert nd._ICMP_CAVEAT in sec["notes"]


# ── discovery_activity ──────────────────────────────────────────────────────

def _activity_db(profiles=2):
    summary = {"runs": 5, "completed": 3, "partial": 1, "failed": 1, "cancelled": 0, "active": 0,
               "targets": 1270, "responding": 160, "new_devices": 12, "changed": 2, "cred_failures": 9,
               "ready": 7, "avg_ms": 95000.0, "profiles": profiles}
    latest = [
        {"created_at": OCT1_NOON, "started_at": OCT1_NOON + timedelta(seconds=5), "profile": "Core LAN",
         "trigger_type": "scheduled", "status": "completed", "duration_ms": 95000, "total_targets": 254,
         "responding_targets": 40, "new_devices": 5, "changed_devices": 1, "credential_failures": 2},
        {"created_at": OCT1_NOON - timedelta(days=1), "started_at": None, "profile": "Windows servers",
         "trigger_type": "manual", "status": "running", "duration_ms": None, "total_targets": 16,
         "responding_targets": 0, "new_devices": 0, "changed_devices": 0, "credential_failures": 0},
    ]
    by_profile = [
        {"profile": "Core LAN", "runs": 4, "last_run": OCT1_NOON, "last_status": "completed",
         "avg_ms": 3725000.0, "new_devices": 12, "cred_failures": 5},
        {"profile": "Windows servers", "runs": 1, "last_run": OCT1_NOON - timedelta(days=1),
         "last_status": None, "avg_ms": None, "new_devices": 0, "cred_failures": 4},
    ]
    return FakeDB([
        ("count(DISTINCT profile_id) AS profiles", [summary]),
        ("ORDER BY r.created_at DESC LIMIT :lim", lambda sql, p: latest[: p["lim"]]),
        ("array_agg(r.status", by_profile),
    ])


def test_discovery_activity_kpis_runs_and_profiles(monkeypatch):
    db = _activity_db()
    sec = run(monkeypatch, "discovery_activity", db, top_n=2)
    assert db.call("count(DISTINCT profile_id)")[1] == {"f": FRM, "t": TO}
    assert kpi(sec, "Discovery runs")["value"] == "5"
    assert kpi(sec, "Discovery runs")["subtitle"] == "3 completed · 1 failed"
    assert kpi(sec, "Responding hosts")["subtitle"] == "of 1,270 addresses scanned"
    assert kpi(sec, "New devices found")["subtitle"] == "7 ready to import"
    assert kpi(sec, "Credential failures")["accent"] == "danger"
    assert kpi(sec, "Average duration")["value"] == "1 min 35 s"
    donut = chart(sec, "Runs by outcome")["series"]
    assert donut["points"] == [{"label": "Completed", "value": 3}, {"label": "Partial", "value": 1},
                               {"label": "Failed", "value": 1}]
    runs = table(sec, "Latest 2 of 5 discovery runs")
    assert runs["rows"][0] == ["Oct 01 15:00", "Core LAN", "completed", "1 min 35 s", "40 of 254", "5 / 1",
                               "2"]
    assert runs["rows"][1][0] == "Sep 30 15:00"            # created_at when it has not started
    assert runs["rows"][1][2:4] == ["running", "—"]         # no duration while running
    prof = table(sec, "By discovery profile")
    assert prof["rows"][0] == ["Core LAN", "4", "Oct 01 15:00", "completed", "1 h 02 min", "12", "5"]
    assert prof["rows"][1][3:5] == ["unknown", "—"]


def test_discovery_activity_single_profile_and_empty(monkeypatch):
    db = _activity_db(profiles=1)
    sec = run(monkeypatch, "discovery_activity", db)
    assert not any("array_agg(r.status" in c[0] for c in db.calls)
    assert [t["title"] for t in sec["tables"]] == ["Latest 2 of 5 discovery runs"]
    sec = run(monkeypatch, "discovery_activity", FakeDB())
    assert sec["notes"] == ["No discovery runs in this period."]


# ── discovery_unmanaged ─────────────────────────────────────────────────────

def _unmanaged_db(groups=None):
    groups = groups if groups is not None else [
        {"vendor": "Unknown", "device_type": "unknown", "n": 9, "ready": 6, "cred_gaps": 0,
         "last_seen": OCT1_NOON},
        {"vendor": "Cisco", "device_type": "switch", "n": 4, "ready": 1, "cred_gaps": 3, "last_seen": OCT1_NOON},
        {"vendor": "APC", "device_type": "ups", "n": 2, "ready": 2, "cred_gaps": 0, "last_seen": None},
        {"vendor": "Ubiquiti", "device_type": "access_point", "n": 1, "ready": 1, "cred_gaps": 0,
         "last_seen": OCT1_NOON},
    ]
    hosts = [
        {"ip": "10.1.0.12", "name": "sw-edge", "vendor": "Cisco", "device_type": "switch", "model": "C9300",
         "os": "IOS-XE", "credential_status": "invalid", "import_ready": False, "scanned_at": OCT1_NOON,
         "profile": "Core LAN"},
        {"ip": "10.1.0.11", "name": None, "vendor": None, "device_type": None, "model": None, "os": None,
         "credential_status": "not_tested", "import_ready": True, "scanned_at": OCT1_NOON, "profile": None},
        {"ip": "10.1.0.30", "name": "ups-1", "vendor": "APC", "device_type": "ups", "model": None, "os": None,
         "credential_status": "valid", "import_ready": False, "scanned_at": OCT1_NOON, "profile": "Core LAN"},
    ]
    return FakeDB([
        ("AS cred_gaps", groups),
        ("ORDER BY l.scanned_at DESC, l.ip_address LIMIT :lim", lambda sql, p: hosts[: p["lim"]]),
    ])


def test_discovery_unmanaged_shadow_inventory(monkeypatch):
    db = _unmanaged_db()
    sec = run(monkeypatch, "discovery_unmanaged", db, top_n=2, days=14)
    sql, params = db.call("AS cred_gaps")
    assert "DISTINCT ON (r.ip_address)" in sql and "r.status IN (" in sql and "'failed'" not in sql
    assert "NOT l.imported" in sql and "discovery_ignored_devices" in sql
    assert "FROM devices d" in sql and "FROM servers sv" in sql
    since = params["since"]
    assert abs((datetime.now(timezone.utc) - timedelta(days=14) - since).total_seconds()) < 60
    assert db.call("LIMIT :lim")[1]["lim"] == 2

    assert kpi(sec, "Unmanaged devices")["value"] == "16"
    assert kpi(sec, "Unmanaged devices")["subtitle"] == "Seen in the last 14 day(s)"
    assert kpi(sec, "Vendor identified")["value"] == "43.75%"
    assert kpi(sec, "Ready to import")["value"] == "10"
    assert kpi(sec, "Credentials failed")["value"] == "3"
    assert [p["label"] for p in chart(sec, "Unmanaged devices by vendor")["series"]["points"]] == [
        "Unknown", "Cisco", "APC", "Ubiquiti"]
    assert chart(sec, "Unmanaged devices by type")["series"]["points"][2] == {"label": "UPS", "value": 2}
    groups = table(sec, "By vendor and type")
    assert groups["title"] == "By vendor and type (top 2 of 4)"
    assert groups["rows"][1] == ["Cisco", "Switch", "4", "1", "3", "Oct 01 15:00"]
    hosts = table(sec, "Most recently seen 2 of 16 unmanaged hosts")
    assert hosts["rows"][0] == ["10.1.0.12", "sw-edge", "Cisco", "Switch · C9300", "IOS-XE", "Core LAN",
                                "Oct 01 15:00", "credentials failed"]
    assert hosts["rows"][1] == ["10.1.0.11", "—", "—", "Unknown", "—", "—", "Oct 01 15:00", "ready to import"]


def test_discovery_unmanaged_folds_types_and_empty(monkeypatch):
    many = [{"vendor": f"V{i}", "device_type": f"type_{i}", "n": 10 - i, "ready": 0, "cred_gaps": 0,
             "last_seen": OCT1_NOON} for i in range(8)]
    sec = run(monkeypatch, "discovery_unmanaged", _unmanaged_db(groups=many))
    pts = chart(sec, "Unmanaged devices by type")["series"]["points"]
    assert len(pts) == 6 and pts[-1] == {"label": "Other", "value": 5 + 4 + 3}
    sec = run(monkeypatch, "discovery_unmanaged", FakeDB(), days=7)
    assert sec["notes"] == ["Discovery found no unmanaged hosts in the last 7 day(s)."]


# ── discovery_credential_gaps ───────────────────────────────────────────────

def _gaps_db():
    agg = [
        {"credential_status": "invalid", "managed": True, "profile": "Core LAN", "n": 2},
        {"credential_status": "invalid", "managed": False, "profile": "Core LAN", "n": 3},
        {"credential_status": "permission_issue", "managed": True, "profile": "Windows servers", "n": 1},
    ]
    hosts = [
        {"ip": "10.1.0.50", "name": "app01", "vendor": "Microsoft", "device_type": "server",
         "credential_status": "permission_issue", "scanned_at": OCT1_NOON, "profile": "Windows servers",
         "managed": True, "protocols": ["WINRM", "WMI"]},
        {"ip": "10.1.0.12", "name": None, "vendor": None, "device_type": None, "credential_status": "invalid",
         "scanned_at": OCT1_NOON, "profile": None, "managed": False, "protocols": []},
    ]
    return FakeDB([
        ("GROUP BY 1, 2, 3", agg),
        ("AS protocols", lambda sql, p: hosts[: p["lim"]]),
    ])


def test_discovery_credential_gaps(monkeypatch):
    db = _gaps_db()
    sec = run(monkeypatch, "discovery_credential_gaps", db, top_n=2)
    sql = db.call("GROUP BY 1, 2, 3")[0]
    # Latest observation that actually tested credentials; the run's own failure definition.
    assert "r.credential_status <> 'not_tested'" in sql
    assert "IN ('invalid', 'permission_issue')" in sql and "'partial'" not in sql
    assert "FROM servers sv" in sql
    assert "raw_data" not in sql                             # JSON is read for the listed hosts only

    assert kpi(sec, "Hosts with credential failures")["value"] == "6"
    assert kpi(sec, "Invalid credentials")["value"] == "5"
    assert kpi(sec, "Permission denied")["value"] == "1"
    assert kpi(sec, "Already in inventory")["value"] == "3"
    assert chart(sec, "Credential failures by discovery profile")["series"]["points"] == [
        {"label": "Core LAN", "value": 5}, {"label": "Windows servers", "value": 1}]
    t = table(sec, "Hosts with credential failures")
    assert t["title"] == "Hosts with credential failures, inventory first (top 2 of 6)"
    assert t["rows"][0] == ["10.1.0.50", "app01", "Microsoft · Server", "Permission denied", "WinRM, WMI", "yes",
                            "Windows servers", "Oct 01 15:00"]
    assert t["rows"][1] == ["10.1.0.12", "—", "—", "Invalid credentials", "—", "no", "—", "Oct 01 15:00"]
    assert any("secrets are never included" in n for n in sec["notes"])


def test_discovery_credential_gaps_empty(monkeypatch):
    sec = run(monkeypatch, "discovery_credential_gaps", FakeDB(), days=90)
    assert sec["notes"] == ["No credential failures among hosts discovery tested in the last 90 day(s)."]


# ── shared helpers ──────────────────────────────────────────────────────────

def test_availability_and_latency_formatting():
    assert nd._avail(None) == "—"
    assert nd._avail(99.999) == "99.99%"
    assert nd._avail(100.0) == "100.00%"
    assert nd._avail(97.1) == "97.10%"
    assert [nd._ms(v) for v in (None, 0.42, 8.42, 24.4, 180.4, 1520.0)] == [
        "—", "0.42 ms", "8.4 ms", "24 ms", "180 ms", "1.52 s"]
    assert nd._delta_pts(None, 99.0) == ""
    assert nd._delta_pts(99.5, 99.0) == "▲ 0.50 pts vs previous period"
    assert nd._type_label("load_balancer") == "Load balancer" and nd._type_label(None) == "Unknown"


def test_local_bucket_grid_and_sql_alignment():
    from types import SimpleNamespace
    ctx = SimpleNamespace(tz_name="Asia/Riyadh", frm=FRM, to=TO)
    days = nd._bucket_starts(ctx, 86400)
    assert days == [FRM_UTC + timedelta(days=i) for i in range(7)]
    two_hours = nd._bucket_starts(ctx, 7200)
    assert len(two_hours) == 84 and two_hours[1] - two_hours[0] == timedelta(hours=2)
    # The Postgres expression is anchored at local midnight, with the zone bound.
    assert ":org_tz" in nd._pg_bucket("s.run_at", 7200) and "7200" in nd._pg_bucket("s.run_at", 7200)
    assert nd._pg_bucket("s.run_at", 86400) == "(date_trunc('day', (s.run_at AT TIME ZONE :org_tz)) " \
                                               "AT TIME ZONE :org_tz)"
