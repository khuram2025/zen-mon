"""Interface section pack: utilization (95th percentile), errors/discards/flaps,
traffic trend and volume, down/unused ports, inventory and status changes.

Every test runs the real section through report_sections.build_sections with
fake Postgres/ClickHouse handles that answer canned rows keyed by an SQL
substring and record every query, so the assertions cover the SQL contract
(device filter, table choice, reset-safe pass) as well as the output."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import interfaces as pack

D1 = "11111111-1111-1111-1111-111111111111"
D2 = "22222222-2222-2222-2222-222222222222"
RIYADH = Branding(timezone="Asia/Riyadh")


# ── fakes ───────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _pick(rules, sql, params):
    for sub, rows in rules:
        if sub in sql:
            return rows(params) if callable(rows) else rows
    return []


class FakeDB:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        return _Result(_pick(self.rules, sql, params or {}))

    def begin_nested(self):
        return _Nested()

    def sql(self, sub):
        return [c for c in self.calls if sub in c[0]]


class _CHResult:
    def __init__(self, rows):
        self.result_rows = list(rows)
        self.column_names = ()


class FakeCH:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        return _CHResult(_pick(self.rules, sql, parameters or {}))

    def sql(self, sub):
        return [c for c in self.calls if sub in c[0]]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def run(sid, monkeypatch, db, ch, *, options=None, days=7, end_days_ago=0, device_ids=(D1, D2)):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    to = _now() - timedelta(days=end_days_ago)
    frm = to - timedelta(days=days)
    filters = {"device_ids": list(device_ids)} if device_ids else None
    out = asyncio.run(rs.build_sections(db, [{"id": sid, "options": options or {}}], frm, to, filters,
                                        requester=None, branding=RIYADH))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["headers"], row)
    return sec


def kpi(sec, label):
    for k in sec["kpis"]:
        if k["label"] == label:
            return k
    raise AssertionError(f"no KPI {label!r} in {[k['label'] for k in sec['kpis']]}")


# Monitored inventory: D1 = core switch, D2 = edge router.
INVENTORY = [
    # device, idx, hostname, ip, if_name, alias, descr, if_speed, configured, admin, oper, tags
    (D1, 1, "core-sw1", "10.0.0.1", "Gi0/1", "Uplink to core", "GigabitEthernet0/1", 1_000_000_000, None,
     "up", "up", ["Core"]),
    (D1, 2, "core-sw1", "10.0.0.1", "Gi0/2", None, None, 1_000_000_000, 100_000_000, "up", "up", []),
    (D2, 5, "edge-rtr", "10.0.0.2", "WAN1", "", None, 0, None, "up", "up", []),
    (D2, 6, "edge-rtr", "10.0.0.2", "Te1/1", None, None, None, None, "down", "down", ["WAN"]),
]
OVERRIDES = [(D2, 5, 50_000_000, 20_000_000, "wan")]
PG_RULES = [("netflow_interface_overrides", OVERRIDES),
            ("di.monitored = TRUE AND di.device_id = ANY", INVENTORY)]


# ── interface_utilization_p95 ───────────────────────────────────────────────

def _util_rows():
    return [
        # device, idx, avg_in, avg_out, p95_in, p95_out, max_in, max_out
        (UUID(D1), 1, 400e6, 90e6, 850e6, 200e6, 990e6, 300e6),
        (UUID(D1), 2, 10e6, 30e6, 30e6, 60e6, 80e6, 95e6),
        (UUID(D2), 5, 4e6, 9e6, 10e6, 18e6, 30e6, 19e6),
        (UUID(D2), 6, 1e6, 1e6, 2e6, 2e6, 3e6, 3e6),
        (UUID(D2), 7, 1e9, 1e9, 1e9, 1e9, 1e9, 1e9),  # not monitored: never listed
    ]


def test_utilization_ranks_by_p95_of_effective_speed(monkeypatch):
    ch = FakeCH([("quantile(0.95)", _util_rows())])
    db = FakeDB(PG_RULES)
    sec = run("interface_utilization_p95", monkeypatch, db, ch)

    (sql, params), = ch.calls
    assert "FROM zenplus.snmp_if_metrics WHERE" in sql and "snmp_if_metrics_5m" not in sql
    assert "device_id IN %(dev_ids)s" in sql and params["dev_ids"] == [D1, D2]
    inv_sql, inv_params = db.sql("di.monitored = TRUE")[0]
    assert inv_params["dev_ids"] == [D1, D2]

    assert sec["title"] == "Top 25 Interfaces by 95th-Percentile Utilization"
    assert kpi(sec, "Interfaces measured")["value"] == "4"
    assert kpi(sec, "Interfaces measured")["subtitle"] == "3 with a known speed"
    assert kpi(sec, "At or above 80%")["value"] == "2"
    top = kpi(sec, "Highest 95th percentile")
    assert top["value"] == "90.0%" and top["subtitle"] == "edge-rtr · WAN1" and top["accent"] == "danger"
    assert kpi(sec, "Speed unknown")["value"] == "1"

    rows = sec["tables"][0]["rows"]
    # WAN1: NetFlow override 50/20 Mbps, outbound 18 Mbps = 90 % (busier direction).
    assert rows[0] == ["edge-rtr", "WAN1", "50 Mbps / 20 Mbps", "10.0 Mbps", "18.0 Mbps", "30.0 Mbps",
                       "45.0%", "90.0%"]
    # Gi0/1 on SNMP speed; alias shown with the name.
    assert rows[1][1] == "Gi0/1 (Uplink to core)" and rows[1][2] == "1 Gbps" and rows[1][7] == "85.0%"
    # Gi0/2 uses the configured circuit speed (100 Mbps), not ifSpeed.
    assert rows[2][2] == "100 Mbps" and rows[2][7] == "60.0%"
    # Unknown speed: utilization is not measured, never 0 %.
    assert rows[3][:2] == ["edge-rtr", "Te1/1"] and rows[3][6:] == ["—", "—"]
    assert len(rows) == 4  # the unmonitored ifIndex 7 is left out
    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "rank" and chart["unit"] == "%"
    assert [p["value"] for p in chart["points"]] == [90.0, 85.0, 60.0]
    assert any("95th percentile of every poll" in n for n in sec["notes"])
    assert any("Speeds used: 1 configured, 1 NetFlow override, 1 SNMP." == n for n in sec["notes"])


def test_utilization_options_direction_top_n_threshold_and_wan(monkeypatch):
    ch = FakeCH([("quantile(0.95)", _util_rows())])
    sec = run("interface_utilization_p95", monkeypatch, FakeDB(PG_RULES), ch,
              options={"direction": "in", "top_n": 2})
    assert sec["title"] == "Top 2 Interfaces by 95th-Percentile Utilization (Inbound)"
    rows = sec["tables"][0]["rows"]
    assert [r[1] for r in rows] == ["Gi0/1 (Uplink to core)", "Gi0/2"]  # 85 %, then 30 % inbound
    assert rows[1][7] == "30.0%"
    assert "top 2 of 4 interfaces" in sec["tables"][0]["title"]

    sec = run("interface_utilization_p95", monkeypatch, FakeDB(PG_RULES), FakeCH([("quantile", _util_rows())]),
              options={"only_above": True, "threshold": 88})
    assert sec["title"] == "Interfaces at or above 88% Utilization (95th Percentile)"
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["WAN1"]
    assert kpi(sec, "At or above 88%")["value"] == "1"

    # WAN links: configured circuit speed, NetFlow role WAN or a "wan" tag (any case).
    sec = run("interface_utilization_p95", monkeypatch, FakeDB(PG_RULES), FakeCH([("quantile", _util_rows())]),
              options={"links": "wan"})
    assert sec["title"] == "Top 25 WAN Links by 95th-Percentile Utilization"
    assert sorted(r[1] for r in sec["tables"][0]["rows"]) == ["Gi0/2", "Te1/1", "WAN1"]
    assert kpi(sec, "WAN links measured")["value"] == "3"
    assert pack.WAN_RULE in sec["notes"]


def test_utilization_wan_empty_state_explains_the_rule(monkeypatch):
    inventory = [r[:8] + (None,) + r[9:11] + ([],) for r in INVENTORY]  # no configured speeds or tags
    sec = run("interface_utilization_p95", monkeypatch,
              FakeDB([("di.monitored = TRUE", inventory)]), FakeCH([("quantile", _util_rows())]),
              options={"links": "wan"})
    assert sec["tables"] == [] and sec["notes"][0] == pack.WAN_EMPTY


def test_utilization_uses_5_minute_rollup_beyond_raw_retention(monkeypatch):
    ch = FakeCH([("quantile(0.95)(b_in)", _util_rows())])
    sec = run("interface_utilization_p95", monkeypatch, FakeDB(PG_RULES), ch, days=30, end_days_ago=20)
    (sql, params), = ch.calls
    assert "FROM zenplus.snmp_if_metrics_5m" in sql and "sum(avg_in_bps * sample_count)" in sql
    assert params["dev_ids"] == [D1, D2]
    assert sec["tables"][0]["rows"][0][7] == "90.0%"
    assert any("5-minute average rates" in n for n in sec["notes"])


# ── interface_health ────────────────────────────────────────────────────────

def _cheap_rows(params):
    t0 = params["f"]
    t1 = params["t"]
    return [
        # device, idx, d_ie, d_oe, d_id, d_od, d_ip, d_op, maybe_flap, has_reset, first, last
        (UUID(D1), 1, 0, 0, 0, 0, 5_000_000, 5_000_000, 0, 0, t0, t1),
        (UUID(D1), 2, 23_956, 0, 10, 0, 1_000_000, 2_000_000, 1, 1, t0, t1),  # counter reset
        (UUID(D2), 5, 0, 0, 50_000, 0, 1_000_000, 0, 0, 0, t0, t1),
        (UUID(D2), 6, 5_000, 0, 0, 0, 10_000, 0, 0, 0, t0, t1),  # admin down
    ]


PRECISE = [(UUID(D1), 2, 3_393, 0, 10, 0, 1_000_000, 2_000_000, 2)]


def test_health_uses_reset_safe_deltas_for_flagged_interfaces(monkeypatch):
    ch = FakeCH([("lagInFrame", PRECISE), ("uniqExact(oper_status) > 1", _cheap_rows)])
    sec = run("interface_health", monkeypatch, FakeDB(PG_RULES), ch)

    cheap_sql, cheap_params = ch.sql("uniqExact(oper_status) > 1")[0]
    assert "max(in_errors) - min(in_errors)" in cheap_sql and "sum(in_errors" not in cheap_sql
    assert cheap_params["dev_ids"] == [D1, D2]
    precise_sql, precise_params = ch.sql("lagInFrame")[0]
    assert f"(toUUID('{D1}'),2)" in precise_sql and f"'{D2}'" not in precise_sql.split("IN (")[-1]
    assert precise_params["dev_ids"] == [D1, D2] and "device_id IN %(dev_ids)s" in precise_sql

    rows = {r[1]: r for r in sec["tables"][0]["rows"]}
    gi2 = rows["Gi0/2"]
    # 3,393 real errors (not the 23,956 max-min of a reset counter) over
    # 3,003,403 attempted frames = 1,129.72 ppm -> critical; 2 flaps.
    assert gi2 == ["core-sw1", "Gi0/2", "3,393", "1,129.72", "10", "3.33", "2", "critical"]
    assert rows["WAN1"][4:] == ["50,000", "47,619.05", "0", "critical"]
    # Administratively down: counted, never judged.
    assert rows["Te1/1"][-1] == "ok"
    assert "Gi0/1 (Uplink to core)" not in rows  # clean interface is not listed
    assert sec["tables"][0]["rows"][0][1] == "Gi0/2"  # severity, then error rate
    assert kpi(sec, "Interfaces analysed")["value"] == "4"
    assert kpi(sec, "With errors")["value"] == "2"
    assert kpi(sec, "With discards")["value"] == "2"
    assert kpi(sec, "Flapping")["value"] == "1"
    assert kpi(sec, "Critical")["value"] == "2" and kpi(sec, "Critical")["accent"] == "danger"
    donut = sec["charts"][0]["series"]
    assert donut["kind"] == "donut" and {p["label"]: p["value"] for p in donut["points"]} == {
        "Healthy": 2, "Critical": 2}


def test_health_sort_option_and_raw_retention_disclosure(monkeypatch):
    ch = FakeCH([("lagInFrame", PRECISE), ("uniqExact(oper_status) > 1", _cheap_rows)])
    sec = run("interface_health", monkeypatch, FakeDB(PG_RULES), ch,
              options={"sort": "errors", "top_n": 1}, days=45)
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["Te1/1"]  # 5,000 errors
    assert "top 1 of 3 interfaces" in sec["tables"][0]["title"]
    # A 45-day window reads only the 30 days of per-poll counters, and says so.
    _sql, params = ch.calls[0]
    assert params["f"] >= _now() - timedelta(days=30, minutes=5)
    assert any("kept for 30 days" in n for n in sec["notes"])


def test_health_quiet_period_reports_no_issues(monkeypatch):
    def clean(params):
        return [(UUID(D1), 1, 0, 0, 0, 0, 10, 10, 0, 0, params["f"], params["t"])]
    sec = run("interface_health", monkeypatch, FakeDB(PG_RULES), FakeCH([("uniqExact", clean)]))
    assert sec["tables"] == []
    assert sec["notes"][0] == "No interface errors, discards or status flaps in this period."
    assert kpi(sec, "Critical")["value"] == "0"


# ── interface_traffic_trend ─────────────────────────────────────────────────

def test_traffic_trend_charts_in_and_out_for_the_busiest(monkeypatch):
    peak = (_now() - timedelta(days=1)).replace(hour=10, minute=0, second=0)
    t0 = peak - timedelta(hours=4)
    t1 = peak - timedelta(hours=2)
    rank = [
        # device, idx, avg_in, avg_out, max_in, max_out, argmax_in_ts, argmax_out_ts
        (UUID(D1), 1, 500e6, 100e6, 900e6, 200e6, peak, t0),
        (UUID(D1), 2, 50e6, 20e6, 90e6, 60e6, t0, t0),
        (UUID(D2), 5, 5e6, 1e6, 9e6, 2e6, t1, t1),
    ]
    series = [
        (UUID(D1), 1, t0, 400e6, 90e6), (UUID(D1), 1, t1, 450e6, 95e6), (UUID(D1), 1, peak, 600e6, 120e6),
        (UUID(D1), 2, t0, 40e6, 20e6), (UUID(D1), 2, peak, 55e6, 22e6),  # no samples at t1
    ]
    ch = FakeCH([("toStartOfInterval", series), ("argMax(timestamp, max_in_bps)", rank)])
    sec = run("interface_traffic_trend", monkeypatch, FakeDB(PG_RULES), ch, options={"top_n": 2})

    assert sec["title"] == "Traffic Trend of the 2 Busiest Interfaces"
    s_sql, s_params = ch.sql("toStartOfInterval")[0]
    assert "snmp_if_metrics_5m" in s_sql and s_params["org_tz"] == "Asia/Riyadh"
    assert f"(toUUID('{D1}'),1)" in s_sql and f"(toUUID('{D1}'),2)" in s_sql and D2 not in s_sql
    assert s_params["dev_ids"] == [D1, D2]
    inbound, outbound = sec["charts"]
    assert inbound["series"]["kind"] == "line" and inbound["series"]["unit"] == "bps"
    lines = {s["name"]: s["points"] for s in inbound["series"]["series"]}
    assert set(lines) == {"core-sw1 · Gi0/1 (Uplink to core)", "core-sw1 · Gi0/2"}
    gi2 = lines["core-sw1 · Gi0/2"]
    assert [p["v"] for p in gi2] == [40e6, None, 55e6]  # a gap stays null, never 0
    assert gi2[0]["t"].endswith("Z")
    assert [p["v"] for p in outbound["series"]["series"][0]["points"]] == [90e6, 95e6, 120e6]
    assert kpi(sec, "Interfaces charted")["value"] == "2 of 3"
    peak_in = kpi(sec, "Peak inbound")
    assert peak_in["value"] == "900.0 Mbps" and peak_in["subtitle"].endswith("13:00")  # 10:00 UTC in Riyadh
    assert sec["tables"][0]["rows"][0][:5] == ["core-sw1", "Gi0/1 (Uplink to core)", "1 Gbps",
                                              "500.0 Mbps", "100.0 Mbps"]


# ── interface_volume ────────────────────────────────────────────────────────

def test_volume_totals_per_interface_and_node_with_previous_period(monkeypatch):
    slots = 7 * 288

    def volume(params):
        if params["f"] < _now() - timedelta(days=8):  # previous period
            return [(UUID(D1), 1, 4e6 * slots, 0.0, slots)]
        return [
            # device, idx, sum of slot rates in, out, slots seen
            (UUID(D1), 1, 8e6 * slots, 0.0, slots),
            (UUID(D1), 2, 0.0, 8e6 * slots // 2, slots // 2),
            (UUID(D2), 5, 2e6 * slots, 2e6 * slots, slots),
        ]
    ch = FakeCH([("sum(b_in), sum(b_out), count()", volume)])
    sec = run("interface_volume", monkeypatch, FakeDB(PG_RULES), ch)

    cur, prev = ch.calls
    assert prev[1]["t"] == cur[1]["f"] and cur[1]["dev_ids"] == [D1, D2] == prev[1]["dev_ids"]
    assert "snmp_if_metrics_5m" in cur[0] and "sum(avg_in_bps * sample_count) / sum(sample_count)" in cur[0]
    gi1 = 8e6 * slots * 300 / 8  # 8 Mbps for 7 days = 604.8 GB
    assert rs._fmt_bytes(gi1) == "563.3 GB"
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["core-sw1", "Gi0/1 (Uplink to core)", "563.3 GB", "0.0 B", "563.3 GB", "50.0%", "100.0%"]
    assert rows[1][1] == "Gi0/2" and rows[1][-1] == "50.0%"  # half the slots had samples
    total = kpi(sec, "Total transferred")
    assert total["value"] == rs._fmt_bytes(gi1 * 2)
    # Same interface set in the previous period carried 4 Mbps on Gi0/1 only.
    assert total["subtitle"] == "▲ 300.0% vs previous period"
    nodes = sec["tables"][1]
    assert nodes["rows"][0][:2] == ["core-sw1", "2"] and nodes["rows"][1][:2] == ["edge-rtr", "1"]
    assert kpi(sec, "Busiest node")["subtitle"] == "core-sw1"
    assert sec["charts"][0]["series"]["unit"] == "B"


# ── interface_down_ports ────────────────────────────────────────────────────

def _down_rules(now):
    per_node = [("sw1", "10.0.0.1", 48, 20, 10, 4, 3), ("sw2", "10.0.0.2", 24, 22, 2, 0, 0)]
    detail = [
        ("sw1", "10.0.0.1", 5, "Gi1/0/5", "Printer", "GigabitEthernet1/0/5", "up", "down",
         now - timedelta(days=45), now - timedelta(days=400)),
        ("sw1", "10.0.0.1", 7, "Gi1/0/7", None, None, "down", "notPresent", None, now - timedelta(days=100)),
    ]
    return [("LIMIT :lim", detail), ("GROUP BY d.id, d.hostname, d.ip_address", per_node)]


def test_down_ports_lists_idle_ethernet_ports(monkeypatch):
    now = datetime.now(timezone.utc)
    db = FakeDB(_down_rules(now))
    sec = run("interface_down_ports", monkeypatch, db, FakeCH())

    sql, params = db.calls[0]
    assert "di.if_type = ANY(:types)" in sql and params["types"] == [6, 117]
    assert params["dev_ids"] == [D1, D2]
    assert abs((params["cutoff"] - (now - timedelta(days=30))).total_seconds()) < 60
    assert "di.last_up_at IS NULL AND di.first_seen < :cutoff" in sql
    assert sec["title"] == "Ports Not Up for 30+ Days"
    assert kpi(sec, "Ethernet ports")["value"] == "72" and kpi(sec, "Ethernet ports")["subtitle"] == "42 up now"
    idle = kpi(sec, "Not up for 30+ days")
    assert idle["value"] == "12" and idle["subtitle"] == "16.7% of Ethernet ports"
    assert kpi(sec, "Never seen up")["value"] == "4"
    rows = sec["tables"][0]["rows"]
    assert rows[0][:5] == ["sw1", "Gi1/0/5", "Printer", "Enabled", "down"]
    assert rows[0][5] == RIYADH.fmt_date(now - timedelta(days=45)) and rows[0][6] == "45"
    assert rows[1][3:] == ["Disabled", "not present", "Never seen up", "≥ 100"]
    assert "top 2 of 12 ports" in sec["tables"][0]["title"]
    by_node = sec["tables"][1]["rows"]
    assert by_node[0] == ["sw1", "48", "20", "10", "4", "20.8%"]
    assert sec["charts"][0]["series"]["points"][0] == {"label": "sw1", "value": 10}
    detail_sql = db.sql("LIMIT :lim")[0][0]
    assert "ORDER BY COALESCE(di.last_up_at, di.first_seen) ASC" in detail_sql


def test_down_ports_options(monkeypatch):
    now = datetime.now(timezone.utc)
    db = FakeDB(_down_rules(now))
    sec = run("interface_down_ports", monkeypatch, db, FakeCH(),
              options={"days": 90, "include_disabled": False, "port_types": "all", "sort": "node"})
    sql, params = db.calls[0]
    assert ":types" not in sql and "types" not in params
    assert "lower(COALESCE(di.admin_status, '')) NOT IN ('down', '2')" in sql
    assert abs((params["cutoff"] - (now - timedelta(days=90))).total_seconds()) < 60
    assert sec["title"] == "Interfaces Not Up for 90+ Days"
    assert "ORDER BY d.hostname, di.if_index" in db.sql("LIMIT :lim")[0][0]
    assert "Administratively down ports are excluded." in sec["notes"]


def test_down_ports_none_idle(monkeypatch):
    db = FakeDB([("GROUP BY d.id", [("sw1", "10.0.0.1", 8, 8, 0, 0, 0)])])
    sec = run("interface_down_ports", monkeypatch, db, FakeCH())
    assert sec["tables"] == [] and sec["notes"][0] == "No Ethernet ports have been down or unused for 30 days or more."
    assert not db.sql("LIMIT :lim")


# ── interface_inventory ─────────────────────────────────────────────────────

INV_GROUPED = [
    # if_type, effective speed, admin, oper, monitored, count
    (6, 1_000_000_000, "up", "up", True, 40),
    (6, 1_000_000_000, "up", "down", True, 10),
    (6, 1_000_000_000, "down", "down", False, 5),
    (24, None, "up", "up", True, 2),
    (131, 100_000_000, "up", "up", False, 3),
    (999, 10_000_000_000, "up", "lowerlayerdown", True, 1),
]
INV_RULES = [("GROUP BY 1, 2, 3, 4, 5", INV_GROUPED),
             ("jsonb_array_elements_text", [("wan", 4, 4, 4, 400_000_000)]),
             ("ORDER BY count(*) DESC, d.hostname", [("sw1", "10.0.0.1", 40, 30, 5, 35),
                                                     ("rtr1", "10.0.0.9", 21, 15, 0, 18)])]


def test_inventory_breakdowns(monkeypatch):
    db = FakeDB(INV_RULES)
    sec = run("interface_inventory", monkeypatch, db, FakeCH(), options={"top_n": 1})
    assert all(p.get("dev_ids") == [D1, D2] for _s, p in db.calls)
    assert "COALESCE(NULLIF(GREATEST(di.configured_speed_bps, 0), 0)" in db.calls[0][0]
    assert kpi(sec, "Interfaces")["value"] == "61" and kpi(sec, "Interfaces")["subtitle"] == "on 2 node(s)"
    assert kpi(sec, "Monitored")["value"] == "53"
    assert kpi(sec, "Up")["value"] == "45"
    assert kpi(sec, "Admin down")["value"] == "5"
    assert kpi(sec, "Speed unknown")["value"] == "2"
    tables = {t["title"]: t for t in sec["tables"]}
    assert tables["Interfaces by type"]["rows"] == [
        ["Ethernet", "55", "40", "10", "5", "50"], ["Tunnel", "3", "3", "0", "0", "0"],
        ["Loopback", "2", "2", "0", "0", "2"], ["ifType 999", "1", "0", "1", "0", "1"]]
    assert [r[0] for r in tables["Interfaces by speed"]["rows"]] == [
        "100–999 Mbps", "1–9.9 Gbps", "10–24.9 Gbps", "Unknown"]
    assert tables["Interfaces by tag"]["rows"] == [["wan", "4", "4", "4", "400 Mbps"]]
    node = tables["Interfaces by node (top 1 of 2 nodes)"]
    assert node["rows"] == [["sw1", "40", "30", "5", "35"]]
    donuts = {c["title"]: c["series"] for c in sec["charts"]}
    assert {p["label"]: p["value"] for p in donuts["Interfaces by status"]["points"]} == {
        "Up": 45, "Down": 11, "Admin down": 5}


def test_inventory_single_breakdown_and_monitored_only(monkeypatch):
    db = FakeDB(INV_RULES)
    sec = run("interface_inventory", monkeypatch, db, FakeCH(),
              options={"breakdown": "type", "monitored_only": True})
    assert sec["title"] == "Interfaces by Type"
    assert [t["title"] for t in sec["tables"]] == ["Interfaces by type"]
    assert [c["title"] for c in sec["charts"]] == ["Interfaces by type"]
    assert not db.sql("jsonb_array_elements_text")
    assert all("di.monitored = TRUE" in s for s, _p in db.calls)


# ── interface_status_changes ────────────────────────────────────────────────

def test_status_changes_counts_transitions_for_changed_interfaces(monkeypatch):
    last = (_now() - timedelta(days=1)).replace(hour=21, minute=30, second=0)
    day1 = datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=3)))
    day2 = datetime(2026, 10, 6, tzinfo=timezone(timedelta(hours=3)))
    pass1 = [(UUID(D1), 1, 1), (UUID(D1), 2, 2), (UUID(D2), 5, 2), (UUID(D2), 7, 2)]
    precise = [
        # device, idx, changes, went down, came up, last change, up share, status now, change buckets
        (UUID(D1), 2, 6, 3, 3, last, 0.95123, 1, [day1, day1, day2, day2, day2, day2]),
        (UUID(D2), 5, 1, 1, 0, last - timedelta(hours=5), 0.5, 2, [day2]),
    ]
    ch = FakeCH([("groupArrayIf", precise), ("uniqExactIf(oper_status, oper_status != 0)", pass1)])
    sec = run("interface_status_changes", monkeypatch, FakeDB(PG_RULES), ch)

    p_sql, p_params = ch.sql("groupArrayIf")[0]
    assert "oper_status != 0" in p_sql and "lagInFrame(oper_status)" in p_sql
    assert f"(toUUID('{D1}'),2)" in p_sql and f"(toUUID('{D2}'),5)" in p_sql
    assert f"(toUUID('{D1}'),1)" not in p_sql and "),7)" not in p_sql  # unchanged / unmonitored
    assert p_params["org_tz"] == "Asia/Riyadh" and p_params["dev_ids"] == [D1, D2]
    assert kpi(sec, "Interfaces with changes")["value"] == "2"
    assert kpi(sec, "Interfaces with changes")["subtitle"] == "of 3 measured"
    assert kpi(sec, "Status changes")["value"] == "7"
    assert kpi(sec, "Went down")["value"] == "4"
    assert kpi(sec, "Most changes")["subtitle"] == "core-sw1 · Gi0/2"
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["core-sw1", "Gi0/2", "6", "3", "3", RIYADH.fmt_short(last), "95.12%", "up"]
    assert rows[0][5].endswith("00:30")  # 21:30 UTC is 00:30 next day in Riyadh
    assert rows[1][-2:] == ["50.00%", "down"]
    bars = sec["charts"][0]["series"]
    assert bars["kind"] == "bars" and [p["v"] for p in bars["points"]] == [2, 5]
    assert bars["points"][0]["t"] == "2026-10-04T21:00:00Z"


def test_windowed_passes_batch_the_inlined_interface_pairs(monkeypatch):
    monkeypatch.setattr(pack, "PAIR_BATCH", 1)
    pass1 = [(UUID(D1), 2, 2), (UUID(D2), 5, 2)]

    def precise(params):
        return [(UUID(D1), 2, 2, 1, 1, _now(), 0.9, 1, []), (UUID(D2), 5, 1, 1, 0, _now(), 0.5, 2, [])]
    ch = FakeCH([("groupArrayIf", precise), ("uniqExactIf", pass1)])
    run("interface_status_changes", monkeypatch, FakeDB(PG_RULES), ch)
    batches = ch.sql("groupArrayIf")
    assert len(batches) == 2 and all(s.count("toUUID(") == 1 for s, _p in batches)

    ch = FakeCH([("lagInFrame", PRECISE), ("uniqExact(oper_status) > 1",
                                          lambda p: [r[:8] + (1, 0) + r[10:] for r in _cheap_rows(p)])])
    run("interface_health", monkeypatch, FakeDB(PG_RULES), ch)
    assert len(ch.sql("lagInFrame")) == 4  # every flagged interface, one per batch


def test_status_changes_min_changes_and_quiet_period(monkeypatch):
    pass1 = [(UUID(D1), 2, 2), (UUID(D2), 5, 2)]
    precise = [(UUID(D1), 2, 6, 3, 3, _now(), 0.9, 1, []), (UUID(D2), 5, 1, 1, 0, _now(), 0.5, 2, [])]
    sec = run("interface_status_changes", monkeypatch, FakeDB(PG_RULES),
              FakeCH([("groupArrayIf", precise), ("uniqExactIf", pass1)]), options={"min_changes": 2})
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["Gi0/2"]

    ch = FakeCH([("uniqExactIf", [(UUID(D1), 1, 1)])])
    sec = run("interface_status_changes", monkeypatch, FakeDB(PG_RULES), ch)
    assert not ch.sql("groupArrayIf")  # nothing changed: the windowed pass never runs
    assert sec["notes"][0] == "No interfaces changed operational status in this period."
    assert kpi(sec, "Interfaces with changes")["value"] == "0"


# ── metadata, helpers and templates ─────────────────────────────────────────

def test_pack_sections_are_device_scoped_and_need_devices_view():
    ids = ["interface_utilization_p95", "interface_health", "interface_traffic_trend", "interface_volume",
           "interface_down_ports", "interface_inventory", "interface_status_changes"]
    for sid in ids:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "device" and e["requires"] == ("devices.view",), sid
        assert e["category"] == "Interfaces"
    assert rs.SECTION_REGISTRY["interface_down_ports"]["semantics"] == "snapshot"
    assert rs.SECTION_REGISTRY["interface_inventory"]["semantics"] == "snapshot"
    assert rs.SECTION_REGISTRY["interface_health"]["semantics"] == "window"


@pytest.mark.parametrize("bps,label", [(None, "—"), (0, "—"), (1_000_000_000, "1 Gbps"),
                                       (2_500_000_000, "2.5 Gbps"), (1_544_000, "1.54 Mbps"),
                                       (100_000_000_000, "100 Gbps")])
def test_speed_format(bps, label):
    assert pack._fmt_speed(bps) == label


def test_speed_bands_and_states():
    assert pack._band(None) == "Unknown" and pack._band(50e6) == "Below 100 Mbps"
    assert pack._band(1e9) == "1–9.9 Gbps" and pack._band(400e9) == "100 Gbps and above"
    assert pack._state("down", "up") == "Admin down" and pack._state("up", "lowerLayerDown") == "Down"
    assert pack._oper_word("notPresent") == "not present" and pack._oper_word("7") == "down"


def test_templates_registered_and_valid():
    keys = {"top_interfaces_utilization_p95", "interfaces_over_threshold", "interface_errors_discards",
            "interface_traffic_trend_report", "interface_total_traffic", "unused_switch_ports",
            "interface_inventory_report", "interface_status_changes_report", "wan_link_capacity_review"}
    assert keys <= set(rt.TEMPLATES)
    assert rt.validate_catalog() == []
    assert rt.TEMPLATES["top_interfaces_utilization_p95"].title == \
        "Top 25 Interfaces by Utilization (95th Percentile)"
    wan = rt.TEMPLATES["wan_link_capacity_review"]
    assert wan.category == "capacity" and all(e["options"].get("links") == "wan" for e in wan.sections)
    for key in keys:
        assert rt.catalog_entry(rt.TEMPLATES[key])["filterable"] == ["devices"]
        assert rs.REPORT_PRESETS[key]["sections"] == rt.TEMPLATES[key].section_ids
