"""NetFlow section pack (report_packs/netflow_ext): capacity and billing, DSCP,
conversations, countries / ASNs, IP groups, interface volume, findings,
exporter health and TCP flags.

Unit tests only: ClickHouse is a fake client chosen by SQL substring (patched
into the NetFlow query runner), PostgreSQL a fake session, and the capacity /
health services are replaced at their seams. Nothing here connects to a
database or to the collector.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from ipaddress import IPv6Address, ip_network
from types import SimpleNamespace

import pytest

from app.api.v1.netflow import access as nfa
from app.api.v1.netflow import breakdowns as bd
from app.api.v1.netflow import common as C
from app.api.v1.netflow.access import FlowVisibility
from app.services import geoip, netflow_capacity as cap, netflow_classify, netflow_groups, netflow_health
from app.services import netflow_report as nr
from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.netflow_groups import Group

PACK = ("netflow_capacity", "netflow_billing_95th", "netflow_qos_dscp", "netflow_conversations",
        "netflow_countries", "netflow_asns", "netflow_ip_groups", "netflow_interface_traffic",
        "netflow_security_findings", "netflow_exporter_health", "netflow_tcp_flags")
TEMPLATES = ("netflow_conversations_report", "netflow_apps_protocols_report", "netflow_qos_dscp_report",
             "netflow_capacity_forecast_report", "netflow_billing_95th_report", "netflow_sites_groups_report",
             "netflow_country_asn_report", "netflow_security_findings_report", "netflow_exporter_health_report",
             "netflow_tcp_flags_report")

ADMIN = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"system.admin"}))
NO_FLOW = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"dashboard.view"}))
SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.0.0.1", "10.0.0.2"}), user_id=None,
                        permissions=frozenset({"netflow.view"}))
RIYADH = Branding(timezone="Asia/Riyadh")
START = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(days=1)
PERIOD = "2026-09-01 15:00 → 2026-09-02 15:00 GMT+3"     # the window in Asia/Riyadh


def v6(ip: str) -> IPv6Address:
    return IPv6Address("::ffff:" + ip)


# ── Fakes ────────────────────────────────────────────────────────────────────

class _CHResult:
    def __init__(self, rows):
        self.result_rows = list(rows)
        self.column_names = ()
        self.first_row = self.result_rows[0] if self.result_rows else None


class FakeCH:
    """NetFlow ClickHouse double: the first route whose key is in the SQL
    answers (rows, or a callable raising/returning rows); records every call."""

    def __init__(self):
        self.routes: list[tuple[str, object]] = []
        self.calls: list[tuple[str, dict]] = []

    def on(self, key: str, rows) -> "FakeCH":
        self.routes.append((key, rows))
        return self

    def query(self, sql, parameters=None, settings=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        for key, rows in self.routes:
            if key in sql:
                return _CHResult(rows(sql, parameters) if callable(rows) else rows)
        return _CHResult([])

    def command(self, *a, **k):
        return None

    def sql_with(self, key: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if key in c[0]]


class _PgResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)

    def fetchall(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def scalars(self):
        return SimpleNamespace(all=lambda: [r[0] for r in self.rows])

    def mappings(self):
        return self


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for key, rows in self.routes:
            if key in sql:
                return _PgResult(rows)
        return _PgResult([])

    def begin_nested(self):
        return _Nested()

    async def rollback(self):
        return None

    async def commit(self):
        return None


class Ctx(rs.SectionCtx):
    """SectionCtx with a fixed flow visibility and the Riyadh organisation clock."""

    def __init__(self, vis=ADMIN, filters=None, db=None, frm=START, to=END):
        super().__init__(db or FakeDB(), frm, to, filters)
        self._vis = vis
        self.branding = RIYADH

    async def flow_visibility(self):
        return self._vis


def render(sid: str, *, ctx: Ctx | None = None, **options) -> dict:
    ctx = ctx or Ctx()
    ctx.section_id = sid
    ctx.section_options = rs.normalize_options(sid, options)
    return asyncio.run(rs.SECTION_REGISTRY[sid]["fn"](ctx))


def kpi(sec: dict, label: str) -> dict:
    return next(k for k in sec["kpis"] if k["label"] == label)


def table(sec: dict, start: str) -> dict:
    return next(t for t in sec["tables"] if (t.get("title") or "").startswith(start))


@pytest.fixture
def ch(monkeypatch):
    fake = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: fake)
    C._retention_cache.update(value=None, at=0.0)

    async def nothing(*a, **k):
        return None

    async def no_flow_exporters(*a, **k):
        return frozenset({"10.0.0.1", "10.0.0.2"})

    monkeypatch.setattr(netflow_classify, "load_rules", nothing)
    monkeypatch.setattr(netflow_groups, "load_groups", nothing)
    monkeypatch.setattr(nfa, "flow_exporters", no_flow_exporters)
    yield fake
    C._retention_cache.update(value=None, at=0.0)


def _shape(sec: dict) -> None:
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"]), t["title"]
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["title"], row)
            assert all(isinstance(c, str) for c in row), row
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)


# ── Registration, permissions and templates ─────────────────────────────────

def test_pack_sections_are_netflow_scoped_traffic_sections():
    for sid in PACK:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "netflow" and e["category"] == "Traffic", sid
        assert e["requires"] == ("netflow.view",), sid
        assert e["fn"].__module__ == "app.services.report_packs.netflow_ext", sid
    assert set(PACK) <= rs.NETFLOW_SCOPED_SECTIONS <= rs.SCOPE_SAFE_SECTIONS


def test_pack_templates_are_valid_and_scopable():
    assert rt.validate_catalog() == []
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category in ("traffic", "capacity", "security"), key
        assert set(t.audience) <= set(rt.AUDIENCES) and t.icon, key
        entry = rt.catalog_entry(t)
        assert entry["filterable"] == ["netflow"] and entry["requires"] == ["netflow.view"], key
    billing = rt.TEMPLATES["netflow_billing_95th_report"]
    assert billing.sections[0]["options"] == {"percentile": "95", "top_n": 100}
    assert billing.default_period == "previous_month"


def test_without_netflow_permission_no_numbers_and_no_query(ch):
    for sid in PACK:
        sec = render(sid, ctx=Ctx(vis=NO_FLOW))
        assert not sec["kpis"] and not sec["tables"] and not sec["charts"], sid
        assert "netflow.view" in sec["notes"][0], sid
    assert not [c for c in ch.calls if "system." not in c[0]]


def test_orphaned_schedule_owner_is_withheld(ch):
    for sid in PACK:
        ctx = rs.SectionCtx(FakeDB(), START, END, None, requester=nr.ORPHANED_OWNER)
        ctx.section_id, ctx.section_options = sid, rs.normalize_options(sid, {})
        sec = asyncio.run(rs.SECTION_REGISTRY[sid]["fn"](ctx))
        assert not sec["kpis"] and not sec["tables"] and "no longer exists" in sec["notes"][0], sid


def test_flow_database_failure_is_a_note_not_an_error(ch):
    def boom(sql, params):
        raise ConnectionRefusedError("connection refused")

    ch.on("GROUP BY c, sv, proto, sp", boom)
    sec = render("netflow_conversations")
    assert not sec["kpis"] and not sec["tables"]
    assert sec["notes"][-1].startswith("No figures: ") and "unaffected" in sec["notes"][-1]


# ── Conversations ────────────────────────────────────────────────────────────

CONV = [
    (v6("10.0.0.5"), v6("93.184.216.34"), 6, 443, 6000, 60, 3, "Web", 443, 40, 10000, 100, 50),
    (v6("10.0.0.6"), v6("8.8.8.8"), 17, 53, 2500, 25, 30, "DNS", 53, 40, 10000, 100, 50),
]


def test_conversations_top_n_whole_scope_remainder_and_timezone(ch):
    ch.on("GROUP BY c, sv, proto, sp", CONV)
    sec = render("netflow_conversations", top_n=2)
    _shape(sec)
    assert sec["title"] == "Top 2 Conversations"
    assert PERIOD in sec["description"]
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 40 conversations by volume"
    assert t["headers"][5] == "Share of total"
    first, second, rest = t["rows"]
    assert first[:2] == ["10.0.0.5", "93.184.216.34"] and first[2].startswith("TCP/443") and first[3] == "Web"
    assert first[4:] == ["6.00 kB", "60.0%", "3"]
    assert second[2].startswith("UDP/53") and second[5] == "25.0%"
    assert rest == ["All other conversations", "—", "—", "—", "1.50 kB", "15.0%", "17"]   # adds up to 100%
    assert kpi(sec, "Conversations")["value"] == "40"
    assert kpi(sec, "Top 2 share")["value"] == "85.0%"
    assert kpi(sec, "Largest conversation")["subtitle"] == "10.0.0.5 → 93.184.216.34"
    sql, params = ch.sql_with("GROUP BY c, sv, proto, sp")[0]
    assert "ORDER BY b DESC" in sql and params["nf_limit"] == 2
    assert sec["charts"][0]["series"]["kind"] == "rank" and sec["charts"][0]["series"]["unit"] == "kB"


def test_conversations_rank_by_flows(ch):
    ch.on("GROUP BY c, sv, proto, sp", CONV)
    sec = render("netflow_conversations", top_n=2, sort="flows")
    sql, _ = ch.sql_with("GROUP BY c, sv, proto, sp")[0]
    assert "ORDER BY n DESC" in sql
    t = sec["tables"][0]
    assert t["headers"][5] == "Share of flows" and t["title"].endswith("by flows")
    assert t["rows"][0][5] == "6.0%" and t["rows"][1][5] == "60.0%" and t["rows"][2][5] == "34.0%"
    assert sec["charts"][0]["series"]["unit"] == "count"


def test_requester_visibility_reaches_the_query(ch):
    ch.on("GROUP BY c, sv, proto, sp", CONV)
    sec = render("netflow_conversations", ctx=Ctx(vis=SCOPED))
    assert any("Restricted to the 2 flow exporters" in n for n in sec["notes"])
    sql, params = ch.sql_with("GROUP BY c, sv, proto, sp")[0]
    bound = " ".join(str(v) for v in params.values())
    assert "10.0.0.1" in bound and "10.0.0.2" in bound


def test_conversations_empty_scope_is_a_note(ch):
    sec = render("netflow_conversations")
    assert not sec["tables"] and sec["notes"][-1] == "No flow traffic in scope in this period."


# ── DSCP ─────────────────────────────────────────────────────────────────────

def test_dscp_shares_remainder_and_unknown_tos(ch):
    ch.on("GROUP BY dscp", [(0, 5000, 50, 5), (46, 3000, 30, 3), (34, 1000, 10, 1), (-1, 1000, 10, 2)])
    sec = render("netflow_qos_dscp", top_n=2)
    _shape(sec)
    rows = sec["tables"][0]["rows"]
    assert [r[0] for r in rows] == ["BE (Best Effort)", "EF (Voice)", "Other DSCP classes (1)", "DSCP not exported"]
    assert [r[3] for r in rows] == ["50.0%", "30.0%", "10.0%", "10.0%"]          # whole scope, adds up
    assert sec["tables"][0]["title"] == "Top 2 of 3 DSCP classes by volume"
    assert kpi(sec, "Voice (EF)")["value"] == "30.0%"
    assert kpi(sec, "Marked (not best effort)")["value"] == "40.0%"
    dk = kpi(sec, "DSCP known")
    assert dk["value"] == "90.0%" and dk["accent"] == "warning"
    assert any("carries no exported ToS field" in n for n in sec["notes"])
    labels = [p["label"] for p in sec["charts"][0]["series"]["points"]]
    assert labels[-1] == "DSCP not exported"


def test_dscp_no_traffic(ch):
    sec = render("netflow_qos_dscp")
    assert not sec["tables"] and sec["notes"][-1] == "No flow traffic in scope in this period."


# ── TCP flags ────────────────────────────────────────────────────────────────

def _tcp_routes(ch):
    # e, total, flagged, rst, syn_only, ack_only, fin, psh, urg, no_flags
    ch.on("GROUP BY e ORDER BY total DESC", [("10.0.0.1", 1000, 1000, 50, 300, 600, 400, 500, 0, 10),
                                              ("10.0.0.2", 500, 0, 0, 0, 0, 0, 0, 0, 0)])
    t0 = int(START.timestamp())
    ch.on("GROUP BY k ORDER BY k", [(t0, 100, 30, 5), (t0 + 3600, 0, 0, 0)])


def test_tcp_flags_shares_coverage_and_indicators(ch, monkeypatch):
    _tcp_routes(ch)

    async def owners(db, ips):
        return {"10.0.0.1": SimpleNamespace(hostname="edge-1")}

    monkeypatch.setattr(nfa, "resolve_exporter_owners", owners)
    sec = render("netflow_tcp_flags")
    _shape(sec)
    assert kpi(sec, "TCP flows")["value"] == "1,500"
    cov = kpi(sec, "Flags exported")
    assert cov["value"] == "66.7%" and cov["accent"] == "warning"
    syn = kpi(sec, "SYN without ACK")
    assert syn["value"] == "30.00%" and syn["accent"] == "warning"
    rst = kpi(sec, "RST share")
    assert rst["value"] == "5.00%" and rst["subtitle"] == "flags exported for part of TCP"
    classes = {r[0]: r for r in table(sec, "TCP flag classes")["rows"]}
    assert classes["SYN without ACK"][1:3] == ["300", "30.0%"]
    ex = table(sec, "Exporters by TCP flows")["rows"]
    assert ex[0] == ["edge-1 (10.0.0.1)", "1,000", "100.0%", "30.00%", "5.00%", "warning"]
    assert ex[1] == ["10.0.0.2", "500", "0.0%", "—", "—", "unknown"]        # flags not exported: unknown
    series = sec["charts"][0]["series"]
    assert series["kind"] == "line" and series["unit"] == "%"
    assert series["points"][0] == {"t": "2026-09-01T12:00:00Z", "v": 30.0}
    assert series["points"][1]["v"] is None                                 # no flagged flows: a gap
    assert [s["name"] for s in series["series"]] == ["SYN without ACK", "RST"]
    assert any("not exported for 33.3% of TCP flows" in n for n in sec["notes"])
    per_exp, trend = ch.sql_with("GROUP BY e ORDER BY total DESC")[0], ch.sql_with("GROUP BY k ORDER BY k")[0]
    assert "protocol = 6" in per_exp[0]
    assert "INTERVAL 1 HOUR" in trend[0] and trend[1]["org_tz"] == "Asia/Riyadh"


def test_tcp_flags_threshold_option(ch, monkeypatch):
    _tcp_routes(ch)

    async def owners(db, ips):
        return {}

    monkeypatch.setattr(nfa, "resolve_exporter_owners", owners)
    sec = render("netflow_tcp_flags", threshold=40)
    assert kpi(sec, "SYN without ACK")["accent"] == "info"
    assert table(sec, "Exporters")["rows"][0][-1] == "healthy"


def test_tcp_flags_not_applicable_to_other_protocols(ch):
    sec = render("netflow_tcp_flags", ctx=Ctx(filters={"netflow": {"protocol": "17"}}))
    assert not sec["tables"] and "other than TCP" in sec["notes"][-1]
    assert not ch.sql_with("GROUP BY e ORDER BY total DESC")


# ── Interface volume ─────────────────────────────────────────────────────────

def _metas(keys):
    return {(e, i): cap.IfaceMeta(exporter=e, ifindex=i, device_id="d1", device_hostname="edge-1",
                                  device_interfaces=4, found=True, if_name=f"Gi0/{i}",
                                  configured_speed=1_000_000_000) for e, i in keys}


def test_interface_traffic_dedup_ranking_and_rates(ch, monkeypatch):
    ch.on("AS ib, sumIf(bytes, direction = 1) AS ob",
          [("10.0.0.1", 1, 8.64e9, 4.32e9, 7), ("10.0.0.1", 2, 1e9, 3e9, 7)])

    async def resolve(db, keys, **kw):
        return _metas(keys)

    monkeypatch.setattr(cap, "resolve_interfaces", resolve)
    sec = render("netflow_interface_traffic", ctx=Ctx(filters={"netflow": {"protocol": "6"}}), top_n=2)
    _shape(sec)
    assert sec["title"] == "Top 2 Interfaces by Flow Volume"
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 7 interfaces by volume"
    assert t["rows"][0] == ["edge-1 / Gi0/1", "8.64 GB", "4.32 GB", "13.0 GB", "800 kbps", "400 kbps", "1.00 Gbps"]
    assert kpi(sec, "Busiest interface")["subtitle"] == "edge-1 / Gi0/1"
    assert kpi(sec, "Highest average rate")["subtitle"] == "edge-1 / Gi0/1 (in)"
    sql, params = ch.sql_with("AS ib, sumIf(bytes, direction = 1) AS ob")[0]
    assert "flow_direction = 1" in sql and "nf_cap_auto" in sql            # ingress/egress de-duplication
    assert "ORDER BY (ib + ob) DESC" in sql and params["nf_limit"] == 2
    assert "protocol" not in sql                                            # traffic filter not applied...
    assert any("traffic filters (protocol) do not apply" in n for n in sec["notes"])   # ...and disclosed


def test_interface_traffic_sort_option(ch, monkeypatch):
    ch.on("AS ib, sumIf(bytes, direction = 1) AS ob", [("10.0.0.1", 1, 1e9, 1e9, 1)])

    async def resolve(db, keys, **kw):
        return _metas(keys)

    monkeypatch.setattr(cap, "resolve_interfaces", resolve)
    sec = render("netflow_interface_traffic", sort="out")
    assert "ORDER BY ob DESC" in ch.sql_with("AS ib, sumIf")[0][0]
    assert sec["tables"][0]["title"] == "Interfaces by volume transmitted"


# ── IP groups ────────────────────────────────────────────────────────────────

def _group(gid, name, cidr, parent=None):
    n = (ip_network(cidr),)
    return Group(id=gid, name=name, cidrs=n, parent_id=parent, effective=n)


def test_ip_groups_shares_ungrouped_and_overlaps(ch, monkeypatch):
    groups = [_group("g1", "HQ", "10.1.0.0/16"), _group("g2", "Branch", "10.2.0.0/16"),
              _group("g3", "Servers", "10.1.5.0/24"), _group("g4", "HQ Lab", "10.1.9.0/24", parent="g1")]
    monkeypatch.setattr(netflow_groups, "groups_snapshot", lambda: list(groups))
    ch.on("sumIf(x_b, NOT (", [(10000, 100, 50, 1000, 5,
                                3000, 2000, 1000, 4000, 40, 20,
                                1000, 1000, 0, 2000, 20, 10,
                                2500, 500, 0, 3000, 30, 15,
                                100, 100, 0, 200, 2, 1)])
    sec = render("netflow_ip_groups", top_n=2)
    _shape(sec)
    t = sec["tables"][0]
    assert t["title"] == "Top 2 of 4 IP groups by traffic involved"
    assert t["rows"][0] == ["HQ", "3.00 kB", "2.00 kB", "1.00 kB", "4.00 kB", "40.0%", "20"]
    assert t["rows"][1][0] == "Servers" and t["rows"][1][5] == "30.0%"
    assert t["rows"][-1] == ["Traffic touching no group", "—", "—", "—", "1.00 kB", "10.0%", "5"]
    assert kpi(sec, "Busiest group")["value"] == "HQ"
    assert kpi(sec, "Ungrouped traffic")["value"] == "10.0%"
    # HQ and Servers overlap; HQ Lab is HQ's own sub-group (overlap by design, not reported).
    overlap = next(n for n in sec["notes"] if n.startswith("Overlapping groups"))
    assert overlap == "Overlapping groups (their traffic is counted in each): HQ and Servers."
    assert any("do not add up to 100%" in n for n in sec["notes"])


def test_ip_groups_not_configured(ch, monkeypatch):
    monkeypatch.setattr(netflow_groups, "groups_snapshot", lambda: [])
    sec = render("netflow_ip_groups")
    assert not sec["tables"] and "No IP groups are configured" in sec["notes"][-1]


# ── Countries and ASNs ───────────────────────────────────────────────────────

GEO = {"country_db": True, "asn_db": True, "country_db_version": "2026-09", "asn_db_version": "2026-09"}
COUNTRY = {"8.8.8.8": ("US", "United States"), "185.1.1.1": ("SA", "Saudi Arabia")}
ASN = {"8.8.8.8": (15169, "GOOGLE"), "185.1.1.1": (25019, "SAUDINETSTC")}


def _prefix_rows(with_asn: bool):
    # k, pfx, sample, sb, sp, sn, db, dp, dn, n_items, all_b, all_p, all_n
    def r(k, ip, sb, db):
        return (k if with_asn else 0, v6(ip), v6(ip), sb, sb // 100, 1, db, db // 100, 1, 6, 12000, 120, 60)
    return [r(15169, "8.8.8.8", 0, 6000), r(0, "185.1.1.1", 2000, 1000), r(0, "10.1.0.5", 7000, 1000),
            r(0, "1.2.3.4", 1000, 2000)]


@pytest.fixture
def geo(monkeypatch):
    monkeypatch.setattr(bd, "_geoip_status", lambda: dict(GEO))
    monkeypatch.setattr(geoip, "country_of", lambda ip: COUNTRY.get(ip, (None, None)))
    monkeypatch.setattr(geoip, "asn_of", lambda ip: ASN.get(ip, (None, None)))


def test_countries_per_side_shares_and_long_tail_remainder(ch, geo):
    ch.on("ARRAY JOIN [(x_s, toUInt8(1))", _prefix_rows(False))
    sec = render("netflow_countries", top_n=2)
    _shape(sec)
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["Private / local", "7.00 kB", "58.3%", "1.00 kB", "8.3%"]
    assert rows[1] == ["United States (US)", "0 B", "0.0%", "6.00 kB", "50.0%"]
    # Everything not listed -- two smaller rows and the long tail beyond the
    # resolved networks -- is one remainder, so each side adds up to 100%.
    assert rows[2] == ["All other countries", "5.00 kB", "41.7%", "5.00 kB", "41.7%"]
    assert kpi(sec, "Countries")["value"] == "2"
    assert kpi(sec, "Top destination")["value"] == "United States"
    assert kpi(sec, "Top source")["value"] == "Saudi Arabia"
    assert kpi(sec, "Private or unplaced")["value"] == "45.8%"
    assert any("2 least busy networks" in n for n in sec["notes"])
    assert any("(2026-09)" in n for n in sec["notes"])


def test_countries_without_geoip_database(ch, monkeypatch):
    monkeypatch.setattr(bd, "_geoip_status", lambda: {**GEO, "country_db": False})
    sec = render("netflow_countries")
    assert not sec["tables"] and "No GeoIP country database" in sec["notes"][-1]


def test_asns_attribution_and_exporter_as_caveat(ch, geo):
    ch.on("ARRAY JOIN [(x_s, x_sa, toUInt8(1))", _prefix_rows(True))
    # A week whose edges are not on the hour: hour rollups serve the interior.
    frm = datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)
    sec = render("netflow_asns", ctx=Ctx(frm=frm, to=frm + timedelta(days=7)), top_n=3)
    _shape(sec)
    rows = {r[0]: r for r in sec["tables"][0]["rows"]}
    assert rows["AS15169 GOOGLE"][1] == "Exporter (BGP)"
    assert rows["AS25019 SAUDINETSTC"][1] == "GeoIP"
    assert kpi(sec, "Attributed by exporter")["value"] == "30.0%"
    assert kpi(sec, "Unknown or private")["value"] == "55.0%"
    assert any(n.startswith("Hourly rollups do not keep the exporter's AS numbers") for n in sec["notes"])
    assert any("exporter put in the flow record" in n for n in sec["notes"])


# ── Security findings ────────────────────────────────────────────────────────

def _t(minutes):
    return START + timedelta(minutes=minutes)


FINDINGS = [
    ("1", "syn_scan", "high", "high", "open", "SYN scan", "10.0.0.9", None, "10.0.0.1", _t(10), _t(60), 3),
    ("2", "syn_scan", "high", "high", "acknowledged", "SYN scan", "10.0.0.8", None, "10.0.0.1", _t(5), _t(50), 2),
    ("3", "threat_intel", "critical", "high", "open", "Contact with a listed host", "10.0.0.7", "1.2.3.4",
     "10.0.0.2", _t(0), _t(30), 1),
    ("4", "volume_spike", "warning", "medium", "resolved", "Volume spike", "10.0.0.6", None, "10.0.0.1",
     _t(0), _t(20), 1),
    ("5", "port_scan", "low", "low", "open", "Port scan", "10.0.0.5", None, "10.9.9.9", _t(0), _t(10), 1),
]


def test_security_findings_grouped_severity_and_org_timezone(ch):
    db = FakeDB([("FROM netflow_findings", FINDINGS)])
    sec = render("netflow_security_findings", ctx=Ctx(db=db), top_n=2)
    _shape(sec)
    assert kpi(sec, "Open or acknowledged")["value"] == "4"
    assert kpi(sec, "Open or acknowledged")["subtitle"] == "3 distinct detections"
    assert kpi(sec, "Critical or high") == {"label": "Critical or high", "value": "3", "accent": "danger"}
    assert kpi(sec, "Hosts involved")["value"] == "4"
    assert kpi(sec, "Resolved")["value"] == "1"
    items = table(sec, "Top 2 of 3 open findings")["rows"]
    assert items[0] == ["critical", "Contact with a listed host", "open", "10.0.0.7", "1", "Sep 01 15:30"]
    assert items[1] == ["major", "SYN scan", "open", "10.0.0.9, 10.0.0.8", "2", "Sep 01 16:00"]
    kinds = table(sec, "Findings by detection")["rows"]
    assert len(kinds) == 3 and kinds[0][4] == "critical" and kinds[1][2:5] == ["2", "2", "major"]
    assert {p["label"] for p in sec["charts"][0]["series"]["points"]} == {"Critical", "High", "Low"}


def test_security_findings_follow_requester_visibility(ch):
    db = FakeDB([("FROM netflow_findings", FINDINGS)])
    sec = render("netflow_security_findings", ctx=Ctx(vis=SCOPED, db=db))
    titles = [r[1] for r in table(sec, "Open findings")["rows"]]
    assert "Port scan" not in titles                       # its exporter is outside the requester's scope
    assert kpi(sec, "Open or acknowledged")["value"] == "3"


def test_security_findings_none_open(ch):
    sec = render("netflow_security_findings")
    assert not sec["tables"] and kpi(sec, "Open or acknowledged")["value"] == "0"
    assert sec["notes"][-1] == "No open or acknowledged NetFlow detection finding in this period."


# ── Exporter health ──────────────────────────────────────────────────────────

STATUS = {"exporters": [
    {"exporter": "10.0.0.1", "device_hostname": "edge-1", "state": "receiving",
     "last_received_at": "2026-09-02T11:59:30Z", "loss_pct_1h": 0.0, "clock_offset_ms": 120,
     "clock_suspect": False, "sampling_interval": 1000,
     "issues": [{"code": "no_tcp_flags", "severity": "info", "message": "x"}]},
    {"exporter": "10.0.0.2", "state": "silent", "last_received_at": "2026-09-01T22:00:00Z", "loss_pct_1h": None,
     "clock_offset_ms": None, "sampling_interval": None,
     "issues": [{"code": "unmapped", "severity": "info", "message": "x"}]},
    {"exporter": "10.0.0.3", "display_name": "fw-dc", "state": "receiving",
     "last_received_at": "2026-09-02T11:59:50Z", "loss_pct_1h": 2.5, "clock_offset_ms": -75000,
     "clock_suspect": True, "sampling_interval": None,
     "issues": [{"code": "sequence_loss", "severity": "warning", "message": "x"},
                {"code": "clock_skew", "severity": "warning", "message": "x"}]},
], "warnings": [{"code": "collector_unreachable", "message": "The collector's live health endpoint did not answer"}]}
COVERAGE = {"exporters": [{"exporter": "10.0.0.1", "coverage_pct": 99.5, "records": 100, "sampled_records": 100,
                           "max_sampling": 1000},
                          {"exporter": "10.0.0.3", "coverage_pct": 80.0, "records": 50, "sampled_records": 0}],
            "totals": {"coverage_pct": 93.2}}


@pytest.fixture
def health(monkeypatch):
    calls = {}

    async def status_for(db, vis, **kw):
        return {**STATUS, "exporters": [r for r in STATUS["exporters"] if vis.allows(r["exporter"])]}

    async def coverage(nf, win):
        return COVERAGE

    async def totals(since, until, exporters):
        calls["exporters"] = exporters
        return {"totals": {"datagrams": 1000, "sequence_gaps": 12}, "process": None, "collectors": []}

    monkeypatch.setattr(netflow_health, "status_for", status_for)
    monkeypatch.setattr(netflow_health, "collector_totals", totals)
    monkeypatch.setattr(nr, "coverage", coverage)
    return calls


def test_exporter_health_states_issues_and_counters(ch, health):
    sec = render("netflow_exporter_health")
    _shape(sec)
    assert kpi(sec, "Exporters")["value"] == "3"
    assert kpi(sec, "Receiving")["value"] == "2"
    late = kpi(sec, "Late, silent or never")
    assert late["value"] == "1" and late["accent"] == "danger"
    assert kpi(sec, "With problems")["value"] == "2"
    cov = kpi(sec, "Collection coverage")
    assert cov["value"] == "93.2%" and cov["accent"] == "warning"
    rows = sec["tables"][0]["rows"]
    # Silent first, then the receiving exporter with problems; times in Asia/Riyadh.
    assert rows[0] == ["10.0.0.2", "offline", "Sep 02 01:00", "—", "—", "—", "—", "Not mapped to a device"]
    assert rows[1] == ["fw-dc (10.0.0.3)", "online", "Sep 02 14:59", "80.0%", "2.50%", "-75.0 s (suspect)",
                       "unsampled", "Sequence loss, Clock skew"]
    assert rows[2] == ["edge-1 (10.0.0.1)", "online", "Sep 02 14:59", "99.5%", "0.00%", "+0.1 s", "1:1,000",
                       "No TCP flags"]
    counters = {r[0]: r[1] for r in table(sec, "Collector counters")["rows"]}
    assert counters["Datagrams received"] == "1,000" and counters["Records written"] == "—"
    assert counters["Sequence gaps (lost export packets/records)"] == "12"
    assert health["exporters"] is None                          # unrestricted: every exporter
    assert any("did not answer" in n for n in sec["notes"])


def test_exporter_health_only_problems_and_scope(ch, health):
    sec = render("netflow_exporter_health", only_issues=True)
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["10.0.0.2", "fw-dc (10.0.0.3)"]
    pinned = render("netflow_exporter_health", ctx=Ctx(filters={"netflow": {"exporter": "10.0.0.3"}}))
    assert [r[0] for r in pinned["tables"][0]["rows"]] == ["fw-dc (10.0.0.3)"]
    assert health["exporters"] == ["10.0.0.3"]
    scoped = render("netflow_exporter_health", ctx=Ctx(vis=SCOPED))
    assert kpi(scoped, "Exporters")["value"] == "2" and health["exporters"] == ["10.0.0.1", "10.0.0.2"]


# ── Capacity and billing ─────────────────────────────────────────────────────

def _stats(samples, in_p, out_p):
    return {"samples": samples, "in_p": in_p, "in_max": in_p * 1.2, "in_sum": in_p * 0.6 * samples,
            "out_p": out_p, "out_max": out_p * 1.2, "out_sum": out_p * 0.6 * samples,
            "sum_p": in_p + out_p, "sum_max": (in_p + out_p) * 1.2, "sum_sum": (in_p + out_p) * 0.6 * samples,
            "in_bytes": 1e9, "out_bytes": 1e9, "in_packets": 1e6, "out_packets": 1e6, "last_slot": None}


def _meta(idx, speed=None, role=None):
    return cap.IfaceMeta(exporter="10.0.0.1", ifindex=idx, device_id="d1", device_hostname="edge-1",
                         device_interfaces=6, found=True, if_name=f"Gi0/{idx}", configured_speed=speed,
                         override={"role": role} if role else None)


class FakeCapacity:
    def __init__(self):
        self.calls: list[dict] = []
        self.trends: list[tuple] = []

    async def compute(self, window, f, vis, db, *, settings=None, business_hours=False, previous=False,
                      record_meta=True, exclude_provisional=False):
        self.calls.append({"f": f, "settings": settings, "business_hours": business_hours, "previous": previous,
                           "exclude_provisional": exclude_provisional})
        specs = [(1, 1e9, None, _stats(288, 950e6, 100e6)),       # critical (95%)
                 (2, 1e8, None, _stats(288, 10e6, 120e6)),        # over (120% out)
                 (3, 1e9, None, _stats(288, 500e6, 200e6)),       # healthy
                 (4, None, None, _stats(288, 300e6, 50e6)),       # speed unknown
                 (5, 1e9, "ignore", _stats(288, 990e6, 990e6)),   # role ignore: left out
                 (6, 1e9, None, _stats(100, 250e6, 10e6))]        # 34.72% coverage: insufficient
        rows = []
        for idx, speed, role, st in specs:
            row = cap.build_row(st, _meta(idx, speed, role), 288, settings)
            if previous:
                row["previous"] = {"change_pct": 12.5 if idx == 1 else None}
            rows.append(row)
        sw = cap.SlotWindow(window.start, window.end)
        return cap.CapacityResult(sw=sw, expected=288, settings=settings, selector=cap.SlotSelector(), rows=rows,
                                  exporters=[], raw_from=None,
                                  method=cap.method_block(settings, cap.SlotSelector(), business_hours=business_hours))

    async def trend(self, window, exporter, ifindex, vis, db, *, settings=None, business_hours=False):
        self.trends.append((window, exporter, ifindex))
        return {"forecast": {"headline_direction": "out",
                             "out": {"status": "ok", "days_to_warn": 0.0, "days_to_crit": 12.0, "days_to_full": None,
                                     "slope_bps_per_day": 2e6, "r2": 0.8123, "confidence": "high", "days_used": 60},
                             "in": {"status": "not_growing"}}}


@pytest.fixture
def capacity(monkeypatch):
    fake = FakeCapacity()

    async def settings(db):
        return cap.DEFAULT_SETTINGS, {}

    monkeypatch.setattr(cap, "load_settings", settings)
    monkeypatch.setattr(cap, "compute_capacity", fake.compute)
    monkeypatch.setattr(cap, "interface_trend", fake.trend)
    return fake


def test_capacity_states_ranking_change_and_forecast(ch, capacity):
    sec = render("netflow_capacity", ctx=Ctx(filters={"netflow": {"protocol": "6"}}), top_n=3, forecast_top=1)
    _shape(sec)
    assert sec["title"] == "NetFlow Interface Capacity"
    assert PERIOD in sec["description"] and "95th percentile" in sec["description"]
    assert kpi(sec, "Interfaces") == {"label": "Interfaces", "value": "5", "accent": "primary",
                                      "subtitle": "4 with a known speed"}
    assert kpi(sec, "Over or critical")["value"] == "2" and kpi(sec, "Over or critical")["accent"] == "danger"
    assert kpi(sec, "Warning")["value"] == "0" and kpi(sec, "Healthy")["value"] == "1"
    assert kpi(sec, "Not assessable")["value"] == "2"
    t = sec["tables"][0]
    assert t["title"] == "Top 3 of 5 interfaces by utilisation risk"
    assert t["rows"][0] == ["edge-1 / Gi0/2", "100 Mbps", "10.0 Mbps", "120 Mbps", "120.0% out (over speed)",
                            "-20.0 Mbps", "—", "critical"]
    assert t["rows"][1] == ["edge-1 / Gi0/1", "1.00 Gbps", "950 Mbps", "100 Mbps", "95.0% in", "50.0 Mbps",
                            "+12.5%", "critical"]
    assert t["rows"][2][-1] == "healthy"
    call = capacity.calls[0]
    assert call["previous"] and call["exclude_provisional"] and call["f"].protocol is None
    assert any("traffic filters (protocol) do not apply" in n for n in sec["notes"])
    assert any("measured above the interface speed" in n for n in sec["notes"])
    # Runout forecast for the highest-risk interface with a known speed, in the organisation timezone.
    (win, exporter, ifindex), = capacity.trends
    assert (exporter, ifindex) == ("10.0.0.1", 2) and win.tz == "Asia/Riyadh"
    assert win.end - win.start == timedelta(days=90)
    fc = table(sec, "Runout forecast")["rows"][0]
    assert fc[:4] == ["edge-1 / Gi0/2", "out", "2.00 Mbps/day", "Reached"]
    assert fc[4].startswith("12 days (") and fc[5] == "> 2 years"
    assert fc[6] == "high confidence (R² 0.81, 60 days)"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "edge-1 / Gi0/2 (out)", "value": 120.0}


def test_capacity_options_at_risk_business_hours_no_forecast(ch, capacity):
    sec = render("netflow_capacity", include="at_risk", business_hours=True, forecast_top=0)
    assert sec["title"] == "NetFlow Interface Capacity (Business Hours)"
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["edge-1 / Gi0/2", "edge-1 / Gi0/1"]
    assert len(sec["tables"]) == 1 and not capacity.trends
    assert capacity.calls[0]["business_hours"] is True


def test_capacity_no_interfaces(ch, monkeypatch):
    async def settings(db):
        return cap.DEFAULT_SETTINGS, {}

    async def empty(window, f, vis, db, **kw):
        return cap.CapacityResult(sw=cap.SlotWindow(window.start, window.end), expected=288,
                                  settings=kw["settings"], selector=cap.SlotSelector(), rows=[], exporters=[],
                                  raw_from=None, method={})

    monkeypatch.setattr(cap, "load_settings", settings)
    monkeypatch.setattr(cap, "compute_capacity", empty)
    sec = render("netflow_capacity")
    assert not sec["tables"] and sec["notes"][-1] == "No interface carried flow traffic in scope in this period."


def test_billing_grade_percentile_and_direction_options(ch, capacity):
    sec = render("netflow_billing_95th", percentile="90")
    _shape(sec)
    assert sec["title"] == "90th Percentile Bandwidth Billing"
    assert capacity.calls[0]["settings"].percentile == 90 and not capacity.calls[0]["previous"]
    rows = sec["tables"][0]["rows"]
    assert sec["tables"][0]["headers"][2:4] == ["In p90", "Out p90"]
    assert rows[0] == ["edge-1 / Gi0/1", "1.00 Gbps", "950 Mbps", "100 Mbps", "950 Mbps (in)", "95.0%", "100.00%",
                       "Yes"]
    assert [r[0] for r in rows] == ["edge-1 / Gi0/1", "edge-1 / Gi0/3", "edge-1 / Gi0/4", "edge-1 / Gi0/2",
                                    "edge-1 / Gi0/6"]
    assert rows[-1][4] == "—" and rows[-1][6] == "34.72%"
    assert rows[-1][7].startswith("No: Coverage 34.72% is below the 99% billing minimum")
    assert kpi(sec, "Billing-grade")["value"] == "4" and kpi(sec, "Indicative only")["value"] == "1"
    assert kpi(sec, "Highest billable rate")["value"] == "950 Mbps"
    assert kpi(sec, "Sum of billable rates")["value"] == "1.87 Gbps"
    assert any("overrides the configured percentile" in n for n in sec["notes"])
    merged = render("netflow_billing_95th", direction_mode="merged")
    assert merged["tables"][0]["rows"][0][4] == "1.05 Gbps (in + out)" and merged["tables"][0]["rows"][0][5] == "—"


def test_billing_business_hours_option(ch, capacity):
    sec = render("netflow_billing_95th", hours="business")
    assert capacity.calls[0]["business_hours"] is True
    assert "Only business-hours samples count (08:00–18:00 UTC, Mon, Tue, Wed, Thu, Fri)." in sec["notes"]


def test_capacity_and_billing_share_one_computation(ch, capacity):
    ctx = Ctx()

    async def both():
        out = []
        for sid in ("netflow_capacity", "netflow_billing_95th"):
            ctx.section_id, ctx.section_options = sid, rs.normalize_options(sid, {"forecast_top": 0})
            out.append(await rs.SECTION_REGISTRY[sid]["fn"](ctx))
        return out

    cap_sec, bill_sec = asyncio.run(both())
    assert len(capacity.calls) == 1                         # billing reused the capacity result
    assert cap_sec["tables"] and bill_sec["tables"]


def test_pack_sections_render_through_build_sections(ch, capacity, health, monkeypatch):
    """The real render path (permissions, option normalisation, error capture)."""
    monkeypatch.setattr(netflow_groups, "groups_snapshot", lambda: [])
    out = asyncio.run(rs.build_sections(FakeDB(), [{"id": s, "options": {"forecast_top": 0}}
                                                   if s == "netflow_capacity" else s for s in PACK],
                                        START.replace(tzinfo=None), END.replace(tzinfo=None),
                                        requester=None, branding=RIYADH))
    assert [s["section_id"] for s in out] == list(PACK)
    for s in out:
        assert not s.get("error"), (s["section_id"], s["notes"])
        assert s["category"] == "Traffic"
        _shape(s)
