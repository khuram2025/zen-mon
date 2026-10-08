"""Hardware section pack (report_packs/hardware.py) with canned data.

Postgres and ClickHouse are fakes answering by SQL substring; nothing here
connects to a database. Covers CPU/memory ranking (sample weighting, the F5
system-RAM rule, previous-period change), the resource trend (gaps, per-device
lines), environment health (sensor peaks, template states, 'no readings' is
not healthy), HA / VPN / BGP state graded with each template's labels, F5
pools (administrative state kept apart from availability) and certificates,
optics and PoE, firewall sessions and SNMP coverage — plus the device filter,
options, staleness and organisation-timezone formatting.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import hardware as pack

A, B, C, D, E, F, G = (f"{c * 8}-0000-0000-0000-00000000000{c}" for c in "abcdef0")
TO = datetime(2026, 10, 7, 12, 0)          # naive UTC, like the render API
FRM = TO - timedelta(days=7)
P_CISCO, P_F5, P_FGT, P_JNX, P_MT = ("p-cisco", "p-f5", "p-fgt", "p-jnx", "p-mt")


def _now():
    return datetime.now(timezone.utc)


FRESH = _now() - timedelta(minutes=10)
OLD = _now() - timedelta(days=3)

# ── device inventory and templates ─────────────────────────────────────────

# id, name, ip, vendor, model, type, profile_id, profile, profile vendor, snmp, ping, poll_mode
DEVICES = {
    A: (A, "core-sw1", "10.0.0.1", "Cisco", "C9300", "switch", P_CISCO, "Cisco IOS", "Cisco", True, True, "direct"),
    B: (B, "bigip-1", "10.0.0.2", "", "", "other", P_F5, "F5 BIG-IP", "F5", True, True, "direct"),
    C: (C, "fw-1", "10.0.0.3", "Fortinet", "FortiGate-100F", "firewall", P_FGT, "Fortinet FortiGate",
        "Fortinet", True, True, "direct"),
    D: (D, "printer-3f", "10.0.0.4", "", "", "other", None, "", "", False, True, "direct"),
    E: (E, "edge-rtr1", "10.0.0.5", "Juniper", "MX204", "router", P_JNX, "Juniper JunOS", "Juniper",
        True, True, "direct"),
    F: (F, "lab-sw9", "10.0.0.6", "", "", "switch", None, "", "", True, True, "direct"),
    G: (G, "ap-12", "", "Fortinet", "FAP-231F", "access_point", None, "", "", False, False,
        "via_controller"),
}

def lab(**codes):
    return {str(k.lstrip("_")): {"text": t, "sev": s} for k, (t, s) in codes.items()}


STATE_LABELS = lab(_1=("Normal", "ok"), _2=("Warning", "warn"), _3=("Critical", "crit"),
                   _5=("Not Present", "info"))
AVAIL = lab(_0=("Unknown", "info"), _1=("Available", "ok"), _2=("Degraded", "warn"),
            _3=("Offline", "crit"), _4=("No Monitor", "info"))
ENABLED = lab(_1=("Enabled", "ok"), _2=("Disabled", "info"), _3=("Disabled (parent)", "info"))

PROFILES = {
    P_CISCO: [
        {"key": "fans", "name": "Fans", "kind": "table",
         "metrics": [{"key": "cisco_fan_state", "name": "State", "type": "enum", "labels": STATE_LABELS}]},
        {"key": "psu", "name": "Power Supplies", "kind": "table",
         "metrics": [{"key": "cisco_psu_state", "name": "State", "type": "enum", "labels": STATE_LABELS}]},
        {"key": "temperature", "name": "Temperature", "kind": "table",
         "metrics": [{"key": "cisco_temp", "name": "Temperature", "type": "gauge", "unit": "°C",
                      "thresholds": {"warn": 50, "crit": 60}}]},
        {"key": "stack", "name": "Stack Ring", "kind": "scalar",
         "metrics": [{"key": "cisco_stack_ring", "name": "Ring Redundant", "type": "enum",
                      "labels": lab(_1=("Redundant", "ok"), _2=("Open Ring", "warn"))}]},
        {"key": "stack_members", "name": "Stack Members", "kind": "table",
         "metrics": [{"key": "cisco_stack_role", "name": "Role", "type": "enum",
                      "labels": lab(_1=("Master", "ok"), _2=("Member", "ok"))}]},
        {"key": "poe", "name": "PoE Budget", "kind": "table",
         "metrics": [{"key": "cisco_poe_power", "name": "Budget", "type": "gauge", "unit": "W"},
                     {"key": "cisco_poe_consumed", "name": "Consumed", "type": "gauge", "unit": "W"},
                     {"key": "cisco_poe_status", "name": "PSE Status", "type": "enum",
                      "labels": lab(_1=("On", "ok"), _2=("Off", "warn"), _3=("Faulty", "crit"))}]},
    ],
    P_F5: [
        {"key": "memory", "name": "Memory", "kind": "scalar",
         "metrics": [{"key": "f5_tmm_mem_used", "type": "gauge"}, {"key": "f5_tmm_mem_total", "type": "gauge"}]},
        {"key": "fans", "name": "Chassis Fans", "kind": "table", "metrics": [
            {"key": "f5_fan_status", "name": "Status", "type": "enum",
             "labels": lab(_0=("Bad", "crit"), _1=("Good", "ok"))}]},
        {"key": "psus", "name": "Power Supplies", "kind": "table", "metrics": [
            {"key": "f5_psu_status", "name": "Status", "type": "enum",
             "labels": lab(_0=("Bad", "crit"), _1=("Good", "ok"))}]},
        {"key": "virtual_servers", "name": "Virtual Servers", "kind": "table", "metrics": [
            {"key": "f5_vs_avail", "name": "Availability", "type": "enum", "labels": AVAIL},
            {"key": "f5_vs_enabled", "name": "Enabled", "type": "enum", "labels": ENABLED},
            {"key": "f5_vs_conns", "name": "Connections", "type": "gauge", "unit": "conns"},
            {"key": "f5_vs_reason", "name": "Detail", "type": "string"}]},
        {"key": "pools", "name": "Pools", "kind": "table", "metrics": [
            {"key": "f5_pool_active", "name": "Active Members", "type": "gauge", "unit": "members"},
            {"key": "f5_pool_members", "name": "Configured Members", "type": "gauge", "unit": "members"},
            {"key": "f5_pool_avail", "name": "Availability", "type": "enum", "labels": AVAIL},
            {"key": "f5_pool_reason", "name": "Detail", "type": "string"}]},
        {"key": "pool_members", "name": "Pool members", "kind": "table", "metrics": [
            {"key": "f5_member_pool", "name": "Pool", "type": "string"},
            {"key": "f5_member_node", "name": "Node", "type": "string"},
            {"key": "f5_member_port", "name": "Port", "type": "gauge"},
            {"key": "f5_member_avail", "name": "Availability", "type": "enum", "labels": AVAIL},
            {"key": "f5_member_enabled", "name": "Enabled", "type": "enum", "labels": ENABLED},
            {"key": "f5_member_connections", "name": "Connections", "type": "gauge", "unit": "conns"},
            {"key": "f5_member_reason", "name": "Detail", "type": "string"}]},
        {"key": "nodes", "name": "Nodes", "kind": "table", "metrics": [
            {"key": "f5_node_avail", "name": "Availability", "type": "enum", "labels": AVAIL},
            {"key": "f5_node_enabled", "name": "Enabled", "type": "enum", "labels": ENABLED}]},
        {"key": "certificates", "name": "Certificate inventory", "kind": "table", "metrics": [
            {"key": "f5_cert_expiry_text", "name": "Expires (UTC)", "type": "string"},
            {"key": "f5_cert_expiry_epoch", "name": "Expiry timestamp", "type": "gauge", "unit": "s"}]},
        {"key": "ha", "name": "HA & Config Sync", "kind": "scalar", "metrics": [
            {"key": "f5_failover", "name": "Failover State", "type": "enum",
             "labels": lab(_3=("Standby", "ok"), _4=("Active", "ok"), _1=("Offline", "crit"))},
            {"key": "f5_sync", "name": "Sync Status", "type": "enum",
             "labels": lab(_3=("In Sync", "ok"), _4=("Sync Failed", "crit"))}]},
    ],
    P_FGT: [
        {"key": "ha", "name": "High Availability", "kind": "scalar", "metrics": [
            {"key": "fgt_ha_mode", "name": "HA Mode", "type": "enum",
             "labels": lab(_1=("Standalone", "info"), _3=("Active-Passive", "ok"))},
            {"key": "fgt_ha_group", "name": "HA Group", "type": "string"}]},
        {"key": "ha_members", "name": "HA Members", "kind": "table", "metrics": [
            {"key": "fgt_ha_sync", "name": "Sync Status", "type": "enum",
             "labels": lab(_0=("Not Synchronized", "crit"), _1=("Synchronized", "ok"))},
            {"key": "fgt_ha_cpu", "name": "CPU", "type": "gauge", "unit": "%",
             "thresholds": {"warn": 80, "crit": 90}}]},
        {"key": "ipsec", "name": "IPsec Tunnels", "kind": "table", "metrics": [
            {"key": "fgt_tun_status", "name": "Status", "type": "enum",
             "labels": lab(_1=("Down", "crit"), _2=("Up", "ok"))},
            {"key": "fgt_tun_in_bps", "name": "Traffic In", "type": "counter", "unit": "bps"}]},
        {"key": "sdwan", "name": "SD-WAN Health", "kind": "table", "metrics": [
            {"key": "fgt_sdwan_state", "name": "State", "type": "enum",
             "labels": lab(_0=("Alive", "ok"), _1=("Dead", "crit"))},
            {"key": "fgt_sdwan_latency", "name": "Latency", "type": "gauge", "unit": "ms",
             "thresholds": {"warn": 150, "crit": 300}}]},
        {"key": "sslvpn", "name": "SSL-VPN (per VDOM)", "kind": "table", "metrics": [
            {"key": "fgt_sslvpn_users", "name": "Logged-in Users", "type": "gauge", "unit": "users"}]},
        {"key": "sensors", "name": "Hardware Sensors", "kind": "table", "metrics": [
            {"key": "fgt_sensor_value", "name": "Value", "type": "gauge"},
            {"key": "fgt_sensor_alarm", "name": "Alarm", "type": "enum",
             "labels": lab(_0=("OK", "ok"), _1=("ALARM", "crit"))}]},
    ],
    P_JNX: [
        {"key": "bgp_peers", "name": "BGP Peers", "kind": "table", "metrics": [
            {"key": "jnx_bgp_state", "name": "State", "type": "enum",
             "labels": lab(_1=("Idle", "warn"), _3=("Active", "warn"), _6=("Established", "ok"))}]},
        {"key": "alarms", "name": "Chassis Alarms", "kind": "scalar", "metrics": [
            {"key": "jnx_alarm_red", "name": "Red Alarm", "type": "enum",
             "labels": lab(_2=("Off", "ok"), _3=("On", "crit"))}]},
    ],
}


def tv(dev, group, metric, instance="", label="", num=None, text_="", unit="", at=None):
    """A device_template_values row."""
    series = f"tpl_{metric}" + (f"_{instance}" if instance else "")
    return (dev, group, metric, instance, label, unit, num, text_, series, at or FRESH)


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
                return _Rows(rows(sql, params or {}) if callable(rows) else rows)
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


def make_db(values=(), sensors=(), devices=None, tpl_fresh=()):
    devs = devices if devices is not None else list(DEVICES.values())

    def scoped(rows, params, idx=0):
        ids = params.get("dev_ids")
        return [r for r in rows if not ids or r[idx] in ids]

    def tpl(sql, params):
        groups = set(params.get("groups") or ())
        return [r for r in scoped(values, params) if r[1] in groups]

    return FakeDB([
        ("LEFT JOIN device_profiles p", lambda s, p: scoped(devs, p)),
        ("p.oid_groups", [(pid, json.dumps(groups)) for pid, groups in PROFILES.items()]),
        ("max(v.updated_at)", lambda s, p: scoped(tpl_fresh, p)),
        ("FROM device_template_values v WHERE v.group_key", tpl),
        ("FROM device_sensors s", lambda s, p: scoped(sensors, p)),
    ])


def run(monkeypatch, db, ch, entry, device_ids=None):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    entries = [entry if isinstance(entry, dict) else {"id": entry}]
    out = asyncio.run(rs.build_sections(db, entries, FRM, TO,
                                        {"device_ids": device_ids} if device_ids else None,
                                        requester=None, branding=Branding(timezone="Asia/Riyadh")))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        assert all(len(r) == len(t["headers"]) for r in t["rows"]), t
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)
    for c in sec["charts"]:
        assert c["series"]["kind"] in ("area", "line", "bars", "stacked", "rank", "donut")
    return sec


def kpi(sec, label_start):
    return next(k for k in sec["kpis"] if k["label"].startswith(label_start))


def table(sec, title_start):
    return next(t for t in sec["tables"] if (t.get("title") or "").startswith(title_start))


def col(t, header):
    i = t["headers"].index(header)
    return [r[i] for r in t["rows"]]


# ── device_resource_utilization ─────────────────────────────────────────────

PEAK_AT = datetime(2026, 10, 5, 9, 0)       # 12:00 in Asia/Riyadh (UTC+3)


def _util_ch(current, prior=()):
    def route(sql, params):
        return current if params["f"] == FRM else prior
    return FakeCH([("FROM zenplus.snmp_metrics_5m", route)])


def test_cpu_ranking_weighting_change_and_timezone(monkeypatch):
    ch = _util_ch(
        # did, key, sum(avg*n), n, p95, peak, peak_at
        [(A, "cpu", 30 * 2000.0, 2000, 55.0, 71.5, PEAK_AT),
         (C, "cpu", 70 * 1000.0, 1000, 92.3, 99.0, PEAK_AT),
         (E, "cpu", 20 * 1000.0, 1000, 25.0, 40.0, PEAK_AT)],
        [(A, "cpu", 28 * 2000.0, 2000), (C, "cpu", 60 * 1000.0, 1000)])
    sec = run(monkeypatch, make_db(), ch,
              {"id": "device_resource_utilization", "options": {"metric": "cpu", "top_n": 2}})
    assert sec["title"] == "Top 2 Devices by CPU Load"
    t = sec["tables"][0]
    assert t["title"].endswith("top 2 of 3 measured devices")
    assert col(t, "Device") == ["fw-1", "core-sw1"]
    assert col(t, "P95 CPU") == ["92.3%", "55.0%"]
    assert col(t, "Peak at") == ["Oct 05 12:00", "Oct 05 12:00"]
    assert col(t, "Avg change") == ["+10.00 pts", "+2.00 pts"]
    assert col(t, "Load") == ["critical", "ok"]
    # Sample-weighted fleet average: (60000 + 70000 + 20000) / 4000 = 37.5.
    assert kpi(sec, "Fleet average")["value"] == "37.5%"
    # Prior (A + C only): 116000 / 3000 = 38.667 → ▼ 1.17 pts.
    assert kpi(sec, "Fleet average")["subtitle"] == "▼ 1.17 pts vs previous period"
    assert kpi(sec, "Devices ≥ 80%")["value"] == "1"
    assert kpi(sec, "Devices measured")["value"] == "3 of 5"   # B and F sent nothing (not measured)
    assert any("2 SNMP-polled device(s)" in n for n in sec["notes"])
    assert sec["charts"][0]["series"]["kind"] == "rank"


def test_cpu_sort_and_threshold_options(monkeypatch):
    ch = _util_ch([(A, "cpu", 30 * 100.0, 100, 55.0, 99.5, PEAK_AT),
                   (C, "cpu", 70 * 100.0, 100, 75.0, 80.0, PEAK_AT)])
    sec = run(monkeypatch, make_db(), ch,
              {"id": "device_resource_utilization",
               "options": {"metric": "cpu", "sort": "peak", "threshold": 50}})
    t = sec["tables"][0]
    assert col(t, "Device") == ["core-sw1", "fw-1"]          # by peak
    assert t["styles"][4] == "pct-bar"
    assert col(t, "Load") == ["warning", "critical"]           # 50% warn, 60% crit
    assert kpi(sec, "Devices ≥ 50%")["value"] == "2"


def test_memory_uses_f5_system_ram_never_legacy_tmm(monkeypatch):
    ch = _util_ch([
        (A, "memory", 60 * 100.0, 100, 62.0, 65.0, PEAK_AT),
        # BIG-IP: legacy 'memory' holds the TMM maximum (95%); system RAM is 40%.
        (B, "memory", 95 * 100.0, 100, 95.0, 96.0, PEAK_AT),
        (B, "f5_system_memory_pct", 40 * 100.0, 100, 41.0, 45.0, PEAK_AT),
        (B, "f5_tmm_memory_pct", 95 * 100.0, 100, 95.0, 96.0, PEAK_AT),
        (B, "f5_swap_memory_pct", 5 * 100.0, 100, 5.0, 6.0, PEAK_AT),
    ])
    sec = run(monkeypatch, make_db(), ch,
              {"id": "device_resource_utilization", "options": {"metric": "memory"}})
    assert sec["title"] == "Top 10 Devices by Memory Utilization"
    t = table(sec, "Devices by")
    rows = {r[0]: r for r in t["rows"]}
    assert rows["bigip-1"][2] == "40.0%" and rows["bigip-1"][3] == "41.0%"
    assert col(t, "Device") == ["core-sw1", "bigip-1"]
    dom = table(sec, "F5 BIG-IP memory domains")
    assert dom["headers"] == ["Device", "System RAM", "TMM", "Non-TMM", "Swap"]
    assert dom["rows"] == [["bigip-1", "40.0% / 45.0%", "95.0% / 96.0%", "—", "5.0% / 6.0%"]]
    assert any("sysGlobalHostMemUsed" in n for n in sec["notes"])


def test_f5_without_system_ram_is_not_measured(monkeypatch):
    ch = _util_ch([(B, "memory", 95 * 100.0, 100, 95.0, 96.0, PEAK_AT),
                   (A, "memory", 50 * 100.0, 100, 50.0, 50.0, PEAK_AT)])
    sec = run(monkeypatch, make_db(), ch,
              {"id": "device_resource_utilization", "options": {"metric": "memory"}})
    assert col(sec["tables"][0], "Device") == ["core-sw1"]
    assert any("1 F5 device(s) have no system RAM samples" in n for n in sec["notes"])


def test_device_filter_reaches_every_query(monkeypatch):
    ch = _util_ch([(A, "cpu", 30 * 100.0, 100, 55.0, 60.0, PEAK_AT)])
    db = make_db()
    sec = run(monkeypatch, db, ch, "device_resource_utilization", device_ids=[A])
    assert kpi(sec, "Devices measured")["value"] == "1 of 1"
    assert ch.calls and all(p.get("dev_ids") == [A] for _s, p in ch.calls)
    assert all("dev_ids" in p for _s, p in db.calls)


def test_no_samples_is_not_measured(monkeypatch):
    sec = run(monkeypatch, make_db(), FakeCH([]), "device_resource_utilization")
    assert sec["tables"] == []
    assert kpi(sec, "Fleet average")["value"] == "—"
    assert sec["notes"][0] == "No device CPU samples in this period."


# ── device_resource_trend ───────────────────────────────────────────────────

def test_trend_fleet_series_gaps_and_f5_memory_condition(monkeypatch):
    b0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    def route(sql, params):
        if "AS b" in sql:
            # bucket, s, n, p95, peak, devices — the 3rd bucket is missing.
            return [(b0, 40 * 10.0, 10, 50.0, 70.0, 2), (b0 + timedelta(hours=2), 20 * 10.0, 10, 30.0, 35.0, 2),
                    (b0 + timedelta(hours=6), 30 * 10.0, 10, 40.0, 45.0, 2)]
        if "GROUP BY did" in sql:
            return [(A,), (B,)]
        return [(25 * 10.0, 10)]          # previous period
    ch = FakeCH([("snmp_metrics_5m", route)])
    sec = run(monkeypatch, make_db(), ch, "device_resource_trend")
    assert sec["title"] == "CPU and Memory Trend"
    cpu_chart = sec["charts"][0]["series"]
    assert [s["name"] for s in cpu_chart["series"]] == ["Average", "95th percentile", "Peak"]
    pts = cpu_chart["series"][0]["points"]
    assert [p["v"] for p in pts] == [40.0, 20.0, None, 30.0]      # gap stays a gap
    assert pts[0]["t"] == "2026-10-01T00:00:00Z"
    assert kpi(sec, "Average CPU")["value"] == "30.0%"
    assert kpi(sec, "Average CPU")["subtitle"] == "▲ 5.00 pts vs previous period"
    assert kpi(sec, "Highest p95 CPU")["subtitle"] == "Oct 01 03:00"
    assert kpi(sec, "Devices reporting")["value"] == "2 of 5"
    mem_sql = [(s, p) for s, p in ch.calls if "f5_system_memory_pct" in s and "AS b" in s]
    assert mem_sql and mem_sql[0][1]["f5_ids"] == [B]
    assert "toString(device_id) NOT IN %(f5_ids)s" in mem_sql[0][0]
    assert all(p.get("org_tz") == "Asia/Riyadh" for s, p in ch.calls if "AS b" in s)


def test_trend_per_device_lines(monkeypatch):
    b0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    def route(sql, params):
        if "AS b" in sql:
            assert params["chosen"] == [C, A]
            return [(b0, C, 80 * 5.0, 5), (b0, A, 20 * 5.0, 5), (b0 + timedelta(hours=2), C, 90 * 5.0, 5)]
        if "ORDER BY s / n DESC" in sql:
            return [(C, 85 * 10.0, 10), (A, 20 * 10.0, 10), (E, 5 * 10.0, 10)]
        return []
    sec = run(monkeypatch, make_db(), FakeCH([("snmp_metrics_5m", route)]),
              {"id": "device_resource_trend", "options": {"metric": "cpu", "breakdown": "devices", "top_n": 2}})
    assert sec["title"] == "CPU Load Trend"
    s = sec["charts"][0]["series"]
    assert [x["name"] for x in s["series"]] == ["fw-1", "core-sw1"]
    assert [p["v"] for p in s["series"][1]["points"]] == [20.0, None]
    assert kpi(sec, "Top device CPU")["subtitle"] == "fw-1"
    assert kpi(sec, "Devices reporting")["value"] == "3 of 5"


# ── hardware_environment ────────────────────────────────────────────────────

ENV_VALUES = [
    tv(A, "fans", "cisco_fan_state", "1", "Fan 1", 1),
    tv(A, "fans", "cisco_fan_state", "2", "Fan 2", 3),          # Critical
    tv(A, "psu", "cisco_psu_state", "1", "PSU 1", 1),
    tv(A, "temperature", "cisco_temp", "1", "Inlet", 41.0, unit="°C"),
    tv(C, "sensors", "fgt_sensor_alarm", "7", "PS1 Status", 1),   # ALARM on a power supply
    tv(E, "alarms", "jnx_alarm_red", num=2),
]


def _env_ch(sql_seen=None):
    def route(sql, params):
        if "AS b" in sql:
            return [(datetime(2026, 10, 1, tzinfo=timezone.utc), 72.0, 45.0)]
        return [
            # did, key, avg, min, max, last, max_at
            (A, "temperature_1", 50.0, 40.0, 72.0, 58.0, PEAK_AT),
            (A, "tpl_cisco_temp_1", 41.0, 39.0, 44.0, 41.0, PEAK_AT),
            (E, "temperature_4", 35.0, 30.0, 39.5, 36.0, PEAK_AT),
            (A, "fan_2", 5000.0, 4800.0, 5200.0, 5100.0, PEAK_AT),
            (A, "sensor_oper_status_3", 3.0, 3.0, 3.0, 3.0, PEAK_AT, PEAK_AT + timedelta(hours=1)),
        ]
    return FakeCH([("snmp_metrics_5m", route)])


SENSORS = [(A, 1, "CPU board", "celsius", "C"), (A, 2, "Fan tray 2", "rpm", "rpm"),
           (A, 3, "PSU 2 temperature", "celsius", "C"), (E, 4, "", "celsius", "C")]


def test_environment_peaks_states_and_no_readings(monkeypatch):
    ch = _env_ch()
    sec = run(monkeypatch, make_db(ENV_VALUES, SENSORS), ch, "hardware_environment")
    temps = table(sec, "Temperature sensors")
    assert col(temps, "Sensor") == ["CPU board", "Inlet Temperature", "Sensor 4"]
    assert temps["rows"][0][2:] == ["58.0 °C", "50.0 °C", "72.0 °C", "Oct 05 12:00", "warning"]
    assert kpi(sec, "Hottest sensor")["value"] == "72.0 °C"
    assert kpi(sec, "Hottest sensor")["subtitle"] == "core-sw1 · CPU board"
    assert kpi(sec, "Sensors ≥ 60 °C")["value"] == "1"
    assert kpi(sec, "Fan faults")["value"] == "1"
    assert kpi(sec, "Power supply faults")["value"] == "1"
    problems = table(sec, "Components needing attention")
    assert ["core-sw1", "Fan 2", "Fan", "Critical", "critical"] in [r[:5] for r in problems["rows"]]
    assert ["fw-1", "PS1 Status", "Power supply", "ALARM", "critical"] in [r[:5] for r in problems["rows"]]
    assert ["core-sw1", "PSU 2 temperature", "Temperature sensor", "Nonoperational", "critical",
            "Oct 05 13:00"] in problems["rows"]
    others = table(sec, "Fan, power and voltage readings")
    assert others["rows"] == [["core-sw1", "Fan tray 2", "Fan", "5,100 RPM", "4,800 RPM", "5,200 RPM"]]
    # BIG-IP and lab-sw9 return nothing: not measured, never healthy.
    assert kpi(sec, "Devices without readings")["value"] == "2"
    without = table(sec, "Devices without hardware readings")
    reasons = dict((r[0], r[3]) for r in without["rows"])
    assert "BIG-IP Virtual Edition" in reasons["bigip-1"]
    assert reasons["lab-sw9"].startswith("No ENTITY-SENSOR")
    assert any("not polled by SNMP" in n for n in sec["notes"])
    # The template temperature series is fetched per series key.
    numeric = next(p for s, p in ch.calls if "match(metric_key" in s and "AS b" not in s)
    assert numeric["tpl"] == ["tpl_cisco_temp_1"]
    line = next(c for c in sec["charts"] if c["series"]["kind"] == "line")
    assert [s["name"] for s in line["series"]["series"]] == ["Hottest sensor", "Average of sensors"]


def test_environment_threshold_option_and_device_filter(monkeypatch):
    ch = _env_ch()
    db = make_db(ENV_VALUES, SENSORS)
    sec = run(monkeypatch, db, ch, {"id": "hardware_environment", "options": {"temp_threshold": 80}},
              device_ids=[A])
    assert kpi(sec, "Sensors ≥ 80 °C")["value"] == "0"
    assert col(table(sec, "Temperature sensors"), "Status")[0] == "ok"
    assert all(p.get("dev_ids") == [A] for _s, p in ch.calls)
    assert all("dev_ids" in p for _s, p in db.calls)
    assert kpi(sec, "Devices with readings")["value"] == "1 of 1"


# ── ha_redundancy_state ─────────────────────────────────────────────────────

HA_VALUES = [
    tv(C, "ha", "fgt_ha_mode", num=3), tv(C, "ha", "fgt_ha_group", text_="7"),
    tv(C, "ha_members", "fgt_ha_sync", "1", "FG100F-A", 1), tv(C, "ha_members", "fgt_ha_cpu", "1", "FG100F-A", 12.0),
    tv(C, "ha_members", "fgt_ha_sync", "2", "FG100F-B", 0),                       # not synchronised
    tv(A, "stack", "cisco_stack_ring", num=2),                                   # open ring …
    tv(A, "stack_members", "cisco_stack_role", "1001", "Switch 1", 1),           # … single member
    tv(E, "std_vrrp_routers", "std_vrrp_state", "10.1", "VRID 1", 3),
    tv(B, "ha", "f5_failover", num=4, at=OLD), tv(B, "ha", "f5_sync", num=3, at=OLD),
]


def test_ha_states_standalone_ring_vrrp_and_stale(monkeypatch):
    sec = run(monkeypatch, make_db(HA_VALUES), FakeCH([]), "ha_redundancy_state")
    t = sec["tables"][0]
    first = t["rows"][0]
    assert first[:3] == ["fw-1", "HA Members", "FG100F-B"]
    assert first[3] == "Not Synchronized" and first[5] == "critical"
    rows = {(r[0], r[1], r[2]): r for r in t["rows"]}
    assert rows[("fw-1", "High Availability", "—")][3] == "HA Mode: Active-Passive · HA Group: 7"
    assert rows[("fw-1", "HA Members", "FG100F-A")][4] == "CPU: 12.0%"
    ring = rows[("core-sw1", "Stack Ring", "—")]
    assert ring[3] == "Standalone — no stack ring" and ring[5] == "info"
    vrrp = rows[("edge-rtr1", "VRRP Routers", "VRID 1")]
    assert vrrp[3] == "Master" and vrrp[5] == "ok"
    assert rows[("bigip-1", "HA & Config Sync", "—")][5] == "stale"
    assert kpi(sec, "Devices with HA data")["value"] == "4"
    assert kpi(sec, "Critical")["value"] == "1"
    assert kpi(sec, "Stale")["value"] == "1"
    assert kpi(sec, "Healthy")["value"] == "2"
    assert any("not refreshed" in n for n in sec["notes"])
    assert sec["charts"][0]["series"]["kind"] == "donut"


def test_ha_problems_only_and_empty(monkeypatch):
    sec = run(monkeypatch, make_db(HA_VALUES), FakeCH([]),
              {"id": "ha_redundancy_state", "options": {"problems_only": True}})
    assert {r[5] for r in sec["tables"][0]["rows"]} == {"critical", "stale"}
    empty = run(monkeypatch, make_db([]), FakeCH([]), "ha_redundancy_state")
    assert empty["notes"] == ["No HA, failover, stack or VRRP data for devices in scope."]


# ── vpn_tunnel_state ────────────────────────────────────────────────────────

VPN_VALUES = [
    tv(C, "ipsec", "fgt_tun_status", "1", "hq-to-branch1", 2), tv(C, "ipsec", "fgt_tun_in_bps", "1", "", 1_500_000.0),
    tv(C, "ipsec", "fgt_tun_status", "2", "hq-to-branch2", 1),
    tv(C, "ipsec", "fgt_tun_status", "3", "hq-to-dr", 2, at=OLD),
    tv(C, "sdwan", "fgt_sdwan_state", "1", "isp-a", 0), tv(C, "sdwan", "fgt_sdwan_latency", "1", "", 180.0),
    tv(C, "sdwan", "fgt_sdwan_state", "2", "isp-b", 1),
    tv(C, "sslvpn", "fgt_sslvpn_users", "root", "root", 37.0),
]


def test_vpn_tunnels_links_and_remote_access(monkeypatch):
    sec = run(monkeypatch, make_db(VPN_VALUES), FakeCH([]), "vpn_tunnel_state")
    assert kpi(sec, "IPsec tunnels")["value"] == "3"
    assert kpi(sec, "Tunnels up")["value"] == "1"
    assert kpi(sec, "Tunnels up")["subtitle"] == "1 stale"
    assert kpi(sec, "Tunnels down")["value"] == "1"
    assert kpi(sec, "SD-WAN links degraded or dead")["value"] == "2"
    assert kpi(sec, "Remote-access users")["value"] == "37"
    t = table(sec, "IPsec tunnels and SD-WAN links")
    assert t["rows"][0][:4] == ["fw-1", "hq-to-branch2", "IPsec", "down"]
    states = {r[1]: (r[3], r[4], r[5]) for r in t["rows"]}
    assert states["hq-to-branch1"] == ("up", "Traffic In: 1.5 Mbps", "ok")
    assert states["isp-a"] == ("up", "Latency: 180 ms", "warning")      # alive but slow
    assert states["hq-to-dr"][0] == "stale"
    ra = table(sec, "Remote-access VPN")
    assert ra["rows"][0][:4] == ["fw-1", "SSL-VPN (per VDOM)", "root", "Logged-in Users: 37 users"]
    donut = sec["charts"][0]["series"]
    assert {p["label"]: p["value"] for p in donut["points"]} == {"Up": 2, "Down": 2, "Stale": 1}


def test_vpn_problems_only_top_n(monkeypatch):
    sec = run(monkeypatch, make_db(VPN_VALUES), FakeCH([]),
              {"id": "vpn_tunnel_state", "options": {"problems_only": True, "top_n": 2}})
    t = sec["tables"][0]
    assert len(t["rows"]) == 2 and t["title"].endswith("top 2 of 4 tunnels and links")


# ── bgp_peer_state ──────────────────────────────────────────────────────────

BGP_VALUES = [
    tv(E, "bgp_peers", "jnx_bgp_state", "192.0.2.1", "192.0.2.1", 6),
    tv(E, "bgp_peers", "jnx_bgp_state", "192.0.2.9", "192.0.2.9", 1),
    tv(A, "std_bgp_peers", "std_bgp_state", "198.51.100.2", "198.51.100.2", 6),
    tv(A, "std_bgp_peers", "std_bgp_remote_as", "198.51.100.2", "198.51.100.2", 65010.0),
    tv(A, "std_bgp_peers", "std_bgp_established_seconds", "198.51.100.2", "198.51.100.2", 93784.0),
    tv(A, "std_bgp_peers", "std_bgp_state", "198.51.100.7", "198.51.100.7", 1),
    tv(A, "std_bgp_peers", "std_bgp_admin", "198.51.100.7", "198.51.100.7", 1),     # stopped
]


def test_bgp_states_admin_down_and_standard_mib(monkeypatch):
    sec = run(monkeypatch, make_db(BGP_VALUES), FakeCH([]), "bgp_peer_state")
    assert kpi(sec, "BGP peers")["value"] == "4"
    assert kpi(sec, "Established")["value"] == "2"
    assert kpi(sec, "Not established")["value"] == "1"
    assert kpi(sec, "Administratively down")["value"] == "1"
    t = sec["tables"][0]
    assert t["rows"][0][:5] == ["edge-rtr1", "192.0.2.9", "—", "Idle", "—"]
    assert t["rows"][0][6] == "warning"
    rows = {r[1]: r for r in t["rows"]}
    assert rows["198.51.100.2"][2:7] == ["AS65010", "Established", "—", "1 d 2 h", "ok"]
    assert rows["198.51.100.7"][3:5] == ["Idle", "Stop"] and rows["198.51.100.7"][6] == "info"
    assert any("IPv6 and VRF peers" in n for n in sec["notes"])


# ── f5_pool_health ──────────────────────────────────────────────────────────

def _member(inst, pool, node, port, avail, enabled, conns=0.0, reason=""):
    return [tv(B, "pool_members", "f5_member_pool", inst, inst, text_=pool),
            tv(B, "pool_members", "f5_member_node", inst, inst, text_=node),
            tv(B, "pool_members", "f5_member_port", inst, inst, float(port)),
            tv(B, "pool_members", "f5_member_avail", inst, inst, avail),
            tv(B, "pool_members", "f5_member_enabled", inst, inst, enabled),
            tv(B, "pool_members", "f5_member_connections", inst, inst, conns),
            tv(B, "pool_members", "f5_member_reason", inst, inst, text_=reason)]


F5_VALUES = [
    *_member("m1", "/Common/web", "10.1.1.1", 80, 1, 1, 120.0, "Pool member is available"),
    *_member("m2", "/Common/web", "10.1.1.2", 80, 3, 1, 0.0, "Monitor http down"),
    *_member("m3", "/Common/web", "10.1.1.3", 80, 3, 2, 0.0, "Disabled by user"),
    tv(B, "virtual_servers", "f5_vs_avail", "vs1", "/Common/vs_web", 1),
    tv(B, "virtual_servers", "f5_vs_enabled", "vs1", "/Common/vs_web", 1),
    tv(B, "virtual_servers", "f5_vs_conns", "vs1", "/Common/vs_web", 1234.0),
    tv(B, "pools", "f5_pool_avail", "p1", "/Common/web", 2),
    tv(B, "pools", "f5_pool_active", "p1", "/Common/web", 1.0),
    tv(B, "pools", "f5_pool_members", "p1", "/Common/web", 3.0),
    tv(B, "nodes", "f5_node_avail", "10.1.1.2", "10.1.1.2", 3),
    tv(B, "nodes", "f5_node_enabled", "10.1.1.2", "10.1.1.2", 1),
]


def test_f5_pool_health_admin_state_kept_apart(monkeypatch):
    sec = run(monkeypatch, make_db(F5_VALUES), FakeCH([]), "f5_pool_health")
    assert kpi(sec, "Virtual servers available")["value"] == "1 of 1"
    assert kpi(sec, "Pools available")["value"] == "0 of 1"
    assert kpi(sec, "Members offline")["value"] == "1"           # the disabled one is not a failure
    assert kpi(sec, "Members disabled")["value"] == "1"
    assert kpi(sec, "Nodes offline")["value"] == "1"
    members = table(sec, "Pool members not available")
    # enrich_f5_groups labels members 'pool · node:port'.
    assert members["rows"] == [
        ["bigip-1", "/Common/web · 10.1.1.2:80", "down", "Enabled", "0", "Monitor http down"],
        ["bigip-1", "/Common/web · 10.1.1.3:80", "down", "Disabled", "0", "Disabled by user"]]
    pools = table(sec, "Pools")
    assert pools["rows"] == [["bigip-1", "/Common/web", "degraded", "—", "1 / 3", "—"]]
    vs = table(sec, "Virtual servers")
    assert vs["rows"][0][2:5] == ["up", "Enabled", "1,234"]
    donut = {p["label"]: p["value"] for p in sec["charts"][0]["series"]["points"]}
    assert donut == {"Down": 2, "Up": 1}


def test_f5_pool_health_show_all(monkeypatch):
    sec = run(monkeypatch, make_db(F5_VALUES), FakeCH([]),
              {"id": "f5_pool_health", "options": {"show_all": True}})
    assert len(table(sec, "Pool members")["rows"]) == 3


# ── f5_certificate_inventory ────────────────────────────────────────────────

def _cert(inst, days):
    epoch = (_now() + timedelta(days=days, hours=1)).timestamp() if days is not None else None
    return tv(B, "certificates", "f5_cert_expiry_epoch", inst, inst, epoch)


def test_certificate_inventory_days_status_and_disclaimer(monkeypatch):
    vals = [_cert("/Common/old.crt", -3), _cert("/Common/api.crt", 10), _cert("/Common/www.crt", 200),
            tv(B, "certificates", "f5_cert_expiry_text", "/Common/odd.crt", "/Common/odd.crt", text_="n/a")]
    sec = run(monkeypatch, make_db(vals), FakeCH([]), "f5_certificate_inventory")
    t = sec["tables"][0]
    assert col(t, "Certificate") == ["/Common/old.crt", "/Common/api.crt", "/Common/www.crt", "/Common/odd.crt"]
    assert col(t, "Days remaining") == ["-3", "10", "200", "—"]
    assert col(t, "Status") == ["expired", "warning", "ok", "unknown"]
    assert kpi(sec, "Expired")["value"] == "1"
    assert kpi(sec, "Expiring within 30 days")["value"] == "1"
    assert kpi(sec, "Next expiry")["value"] == "10 days"
    assert any("does not prove" in n for n in sec["notes"])
    assert "not proof of active TLS use" in rs.SECTION_REGISTRY["f5_certificate_inventory"]["description"]
    wide = run(monkeypatch, make_db(vals), FakeCH([]),
               {"id": "f5_certificate_inventory", "options": {"warn_days": 365, "top_n": 2}})
    assert col(wide["tables"][0], "Status") == ["expired", "warning"]
    assert wide["tables"][0]["title"].endswith("top 2 of 4 certificates")


# ── optics_poe_health ───────────────────────────────────────────────────────

OPTICS_PROFILE = [{"key": "optics", "name": "Optical Modules", "kind": "table", "metrics": [
    {"key": "mt_opt_rx_loss", "name": "RX Loss", "type": "enum"},
    {"key": "mt_opt_temp", "name": "Temperature", "type": "gauge", "unit": "C"},
    {"key": "mt_opt_tx_dbm", "name": "TX Power", "type": "gauge", "unit": "dBm"},
    {"key": "mt_opt_rx_dbm", "name": "RX Power", "type": "gauge", "unit": "dBm"}]}]


def test_optics_low_rx_unlabeled_codes_and_poe_budget(monkeypatch):
    monkeypatch.setitem(PROFILES, P_MT, OPTICS_PROFILE)
    devices = list(DEVICES.values()) + [
        ("99999999-0000-0000-0000-000000000009", "mt-rtr", "10.0.0.9", "MikroTik", "CCR2004", "router",
         P_MT, "MikroTik RouterOS", "MikroTik", True, True, "direct")]
    mt = "99999999-0000-0000-0000-000000000009"
    vals = [tv(mt, "optics", "mt_opt_rx_dbm", "1", "sfp-sfpplus1", -25.5, unit="dBm"),
            tv(mt, "optics", "mt_opt_tx_dbm", "1", "sfp-sfpplus1", -2.1, unit="dBm"),
            tv(mt, "optics", "mt_opt_temp", "1", "sfp-sfpplus1", 48.0, unit="C"),
            tv(mt, "optics", "mt_opt_rx_loss", "1", "sfp-sfpplus1", 1),
            tv(mt, "optics", "mt_opt_rx_dbm", "2", "sfp-sfpplus2", -6.0, unit="dBm"),
            tv(A, "poe", "cisco_poe_power", "1", "1", 370.0, unit="W"),
            tv(A, "poe", "cisco_poe_consumed", "1", "1", 350.0, unit="W"),
            tv(A, "poe", "cisco_poe_status", "1", "1", 1)]
    sec = run(monkeypatch, make_db(vals, devices=devices), FakeCH([]), "optics_poe_health")
    assert kpi(sec, "Optical modules")["value"] == "2"
    assert kpi(sec, "Receive power below -20 dBm")["value"] == "1"
    assert kpi(sec, "Lowest receive power")["value"] == "-25.50 dBm"
    assert kpi(sec, "PoE budget used")["value"] == "94.6%"
    assert kpi(sec, "PoE budget used")["subtitle"] == "350 W of 370 W"
    assert kpi(sec, "PoE at or above 90%")["value"] == "1"
    opt = table(sec, "Optical modules")
    assert opt["rows"][0] == ["mt-rtr", "sfp-sfpplus1", "-25.50 dBm", "-2.10 dBm", "48.0 °C",
                              "RX Loss: code 1", "warning"]
    assert opt["rows"][1][2] == "-6.00 dBm" and opt["rows"][1][4] == "—"
    poe = table(sec, "PoE power sourcing")
    assert poe["rows"][0] == ["core-sw1", "1", "370.0 W", "350.0 W", "94.6%", "On", "warning"]
    assert any("numeric codes without a label map" in n for n in sec["notes"])
    relaxed = run(monkeypatch, make_db(vals, devices=devices), FakeCH([]),
                  {"id": "optics_poe_health", "options": {"poe_threshold": 99, "rx_low_dbm": -30}})
    assert kpi(relaxed, "PoE at or above 99%")["value"] == "0"
    assert kpi(relaxed, "Receive power below -30 dBm")["value"] == "0"


# ── firewall_sessions ───────────────────────────────────────────────────────

def test_firewall_sessions_sources_capacity_and_trend(monkeypatch):
    pan = "88888888-0000-0000-0000-000000000008"
    devices = list(DEVICES.values()) + [
        (pan, "pa-1", "10.0.0.8", "Palo Alto Networks", "PA-3220", "firewall", None, "", "", True, True, "direct")]
    b0 = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def route(sql, params):
        if "AS b" in sql:
            assert set(params["chosen"]) <= {C, pan}
            return [(b0, C, "sessions", 1000.0 * 4, 4), (b0, C, "tpl_fgt_sessions", 1.0, 1),
                    (b0, pan, "tpl_pan_sess_active", 90000.0 * 2, 2)]
        return [
            # did, key, s, n, peak, peak_at, last
            (C, "sessions", 1200.0 * 10, 10, 5000.0, PEAK_AT, 1100.0),
            (C, "tpl_fgt_sessions", 1200.0 * 10, 10, 5000.0, PEAK_AT, 1100.0),
            (pan, "tpl_pan_sess_active", 80000.0 * 10, 10, 92000.0, PEAK_AT, 70000.0),
            (pan, "tpl_pan_sess_max", 100000.0 * 10, 10, 100000.0, PEAK_AT, 100000.0),
        ]
    sec = run(monkeypatch, make_db(devices=devices), FakeCH([("snmp_metrics_5m", route)]),
              "firewall_sessions")
    t = sec["tables"][0]
    assert t["rows"][0] == ["pa-1", "PAN-OS active sessions", "80,000", "92,000", "Oct 05 12:00",
                            "100,000", "92.0%", "critical"]
    assert t["rows"][1] == ["fw-1", "FortiGate sessions", "1,200", "5,000", "Oct 05 12:00", "—", "—",
                            "unknown"]
    assert kpi(sec, "Firewalls reporting sessions")["value"] == "2"
    assert kpi(sec, "Peak concurrent sessions")["value"] == "92,000"
    assert kpi(sec, "Highest session-table use")["value"] == "92.0%"
    assert kpi(sec, "At or above 80% of capacity")["value"] == "1"
    line = sec["charts"][1]["series"]
    assert [s["name"] for s in line["series"]] == ["pa-1", "fw-1"]
    assert line["series"][1]["points"][0]["v"] == 1000.0      # 'sessions', not the template copy
    relaxed = run(monkeypatch, make_db(devices=devices), FakeCH([("snmp_metrics_5m", route)]),
                  {"id": "firewall_sessions", "options": {"threshold": 95, "top_n": 1}})
    assert col(relaxed["tables"][0], "Status") == ["ok"]
    assert relaxed["tables"][0]["title"].endswith("top 1 of 2 firewalls")


def test_firewall_sessions_none(monkeypatch):
    sec = run(monkeypatch, make_db(), FakeCH([]), "firewall_sessions")
    assert sec["notes"][0] == "No firewall session samples in this period."
    assert "1 firewall(s) in scope reported no session count" in sec["notes"][1]


# ── monitoring_coverage ─────────────────────────────────────────────────────

def test_monitoring_coverage_states_and_options(monkeypatch):
    last = {A: _now() - timedelta(minutes=4), C: _now() - timedelta(hours=5),
            E: _now() - timedelta(minutes=2), F: _now() - timedelta(minutes=3)}
    ch = FakeCH([("max(timestamp) AS last_at", [(k, v.replace(tzinfo=None)) for k, v in last.items()])])
    db = make_db(tpl_fresh=[(A, FRESH), (C, OLD)])
    sec = run(monkeypatch, db, ch, "monitoring_coverage")
    assert kpi(sec, "SNMP reporting")["value"] == "3 of 5"
    assert kpi(sec, "SNMP not responding")["value"] == "2"     # fw-1 stale, bigip-1 never
    assert kpi(sec, "No monitoring template")["value"] == "1"  # lab-sw9
    assert kpi(sec, "Ping only")["value"] == "1"
    t = sec["tables"][0]
    assert col(t, "Device") == ["bigip-1", "fw-1", "lab-sw9", "printer-3f"]
    assert col(t, "State") == ["no data", "stale", "ok", "n/a"]
    assert col(t, "Polling")[3] == "Ping"
    assert t["rows"][0][5] == "—"
    assert t["rows"][1][6] != "—"
    assert any("report through their controller" in n for n in sec["notes"])
    donut = {p["label"]: p["value"] for p in sec["charts"][0]["series"]["points"]}
    assert donut["Via controller"] == 1 and donut["SNMP never responded"] == 1
    lax = run(monkeypatch, db, ch, {"id": "monitoring_coverage",
                                    "options": {"stale_hours": 6, "include_ping_only": False}})
    assert col(lax["tables"][0], "Device") == ["bigip-1", "lab-sw9"]


def test_monitoring_coverage_device_filter(monkeypatch):
    ch = FakeCH([])
    db = make_db()
    sec = run(monkeypatch, db, ch, "monitoring_coverage", device_ids=[B])
    assert kpi(sec, "SNMP reporting")["value"] == "0 of 1"
    assert ch.calls and all(p.get("dev_ids") == [B] for _s, p in ch.calls)
    assert all("dev_ids" in p for _s, p in db.calls)


# ── registry and templates ──────────────────────────────────────────────────

SECTIONS = ("device_resource_utilization", "device_resource_trend", "hardware_environment",
            "ha_redundancy_state", "vpn_tunnel_state", "bgp_peer_state", "f5_pool_health",
            "f5_certificate_inventory", "optics_poe_health", "firewall_sessions", "monitoring_coverage")
TEMPLATES = ("top_devices_cpu_load", "top_devices_memory_utilization", "device_cpu_memory_trend",
             "hardware_health_environment", "ha_redundancy_status", "vpn_tunnel_status", "bgp_peer_status",
             "f5_load_balancer_health", "f5_certificate_inventory_report", "optics_poe_health_report",
             "firewall_session_capacity", "snmp_monitoring_coverage")


def test_sections_and_templates_registered():
    for sid in SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "device" and e["requires"] == ("devices.view",), sid
        assert e["category"] != "Traffic"
    snap = {s for s in SECTIONS if rs.SECTION_REGISTRY[s]["semantics"] == "snapshot"}
    assert snap == {"ha_redundancy_state", "vpn_tunnel_state", "bgp_peer_state", "f5_pool_health",
                    "f5_certificate_inventory", "optics_poe_health", "monitoring_coverage"}
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert rs.REPORT_PRESETS[key]["title"] == t.title
        assert set(t.audience) <= set(rt.AUDIENCES)
        assert "devices" in rt.catalog_entry(t)["filterable"]
    assert rt.validate_catalog() == []


@pytest.mark.parametrize("sid", SECTIONS)
def test_every_section_renders_with_devices_but_no_data(sid, monkeypatch):
    sec = run(monkeypatch, make_db(), FakeCH([]), sid)
    assert sec["notes"], sid
