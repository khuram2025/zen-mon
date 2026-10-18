"""Device page data truth: template applicability, missing readings, health
rollup, interface counting, identity resolution and GET /devices/{id}/summary."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from app.api.v1 import devices as devices_api
from app.services import device_insights as di

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

FORTIGATE = [
    {"key": "system", "name": "System Health", "kind": "scalar", "metrics": [
        {"key": "fgt_cpu", "name": "CPU Usage", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}},
        {"key": "fgt_mem", "name": "Memory Usage", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 88}},
        {"key": "fgt_fw_version", "name": "Firmware", "type": "string"},
    ]},
    {"key": "ha", "name": "High Availability", "kind": "scalar", "metrics": [
        {"key": "fgt_ha_mode", "name": "HA Mode", "type": "enum",
         "labels": {"1": {"text": "Standalone", "sev": "info"}, "2": {"text": "Active-Active", "sev": "ok"},
                    "3": {"text": "Active-Passive", "sev": "ok"}}},
    ]},
    {"key": "ha_members", "name": "HA Members", "kind": "table", "metrics": [
        {"key": "fgt_ha_cpu", "name": "CPU", "type": "gauge", "unit": "%"},
        {"key": "fgt_ha_sync", "name": "Sync Status", "type": "enum",
         "labels": {"0": {"text": "Not Synchronized", "sev": "crit"}, "1": {"text": "Synchronized", "sev": "ok"}}},
    ]},
    {"key": "ipsec", "name": "IPsec Tunnels", "kind": "table", "metrics": [
        {"key": "fgt_tun_status", "name": "Status", "type": "enum",
         "labels": {"1": {"text": "Down", "sev": "crit"}, "2": {"text": "Up", "sev": "ok"}}},
        {"key": "fgt_tun_in_bps", "name": "Traffic In", "type": "counter", "unit": "bps"},
    ]},
    {"key": "wifi", "name": "WiFi Controller", "kind": "scalar", "metrics": [
        {"key": "fgt_wifi_clients", "name": "WiFi Clients", "type": "gauge", "unit": "clients"},
        {"key": "fgt_wifi_capacity", "name": "Client Capacity", "type": "gauge", "unit": "clients"},
    ]},
    {"key": "access_points", "name": "FortiAP Access Points", "kind": "table", "metrics": [
        {"key": "fgt_ap_status", "name": "Status", "type": "enum",
         "labels": {"1": {"text": "Offline", "sev": "crit"}, "2": {"text": "Online", "sev": "ok"}}},
        {"key": "fgt_ap_clients", "name": "Clients", "type": "gauge", "unit": "clients"},
        {"key": "fgt_ap_cpu", "name": "CPU", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}},
    ]},
]


def _v(group, metric, num=None, text=None, instance="", label=None):
    return dict(group_key=group, metric_key=metric, instance=instance, series_key=f"tpl_{metric}_{instance}",
                label=label, unit=None, value_num=num, value_text=text, updated_at=NOW)


def _fortigate_values(ha_mode=1, aps_offline=4, aps_online=9):
    vals = [
        _v("system", "fgt_cpu", 13), _v("system", "fgt_mem", 54),
        _v("system", "fgt_fw_version", text="v7.6.7,build3704,260601 (GA.M)"),
        _v("ha", "fgt_ha_mode", ha_mode),
        _v("ha_members", "fgt_ha_cpu", 0, instance="1", label="FG101F"),
        _v("ha_members", "fgt_ha_sync", 0, instance="1", label="FG101F"),
        _v("ipsec", "fgt_tun_status", 1, instance="1", label="ACC_To_Amman"),
        _v("ipsec", "fgt_tun_in_bps", 0, instance="1", label="ACC_To_Amman"),
        _v("ipsec", "fgt_tun_status", 2, instance="2", label="ACC_To_Riyadh"),
        _v("wifi", "fgt_wifi_clients", 136), _v("wifi", "fgt_wifi_capacity", 0),
    ]
    for i in range(aps_offline):
        vals += [_v("access_points", "fgt_ap_status", 1, instance=f"o{i}", label=f"AP-off-{i}"),
                 _v("access_points", "fgt_ap_clients", 0, instance=f"o{i}", label=f"AP-off-{i}"),
                 _v("access_points", "fgt_ap_cpu", 0, instance=f"o{i}", label=f"AP-off-{i}")]
    for i in range(aps_online):
        vals += [_v("access_points", "fgt_ap_status", 2, instance=f"n{i}", label=f"AP-on-{i}"),
                 _v("access_points", "fgt_ap_clients", 15, instance=f"n{i}", label=f"AP-on-{i}"),
                 _v("access_points", "fgt_ap_cpu", 12, instance=f"n{i}", label=f"AP-on-{i}")]
    return vals


def _groups(**kw):
    groups, _ = di.shape_template_groups(FORTIGATE, _fortigate_values(**kw))
    return {g["key"]: g for g in groups}


def test_ha_members_are_not_applicable_on_a_standalone_unit():
    g = _groups(ha_mode=1)["ha_members"]
    assert g["status"] == "na" and g["applicable"] is False
    assert "Standalone" in g["not_applicable_reason"]
    assert not [i for i in di.template_issues(list(_groups(ha_mode=1).values())) if i["group"] == "ha_members"]


def test_ha_members_still_alert_in_a_cluster_or_when_the_mode_is_unknown():
    assert _groups(ha_mode=3)["ha_members"]["status"] == "crit"
    groups, _ = di.shape_template_groups(FORTIGATE, [v for v in _fortigate_values() if v["metric_key"] != "fgt_ha_mode"])
    assert next(g for g in groups if g["key"] == "ha_members")["status"] == "crit"


def test_template_declared_applicability_wins_over_the_builtin_rule():
    tpl = [dict(g) for g in FORTIGATE]
    members = next(g for g in tpl if g["key"] == "ha_members")
    members["applies_when"] = {"group": "ha", "metric": "fgt_ha_mode", "in": [2], "reason": "Only active-active"}
    groups, _ = di.shape_template_groups(tpl, _fortigate_values(ha_mode=3))
    g = next(g for g in groups if g["key"] == "ha_members")
    assert g["applicable"] is False and g["not_applicable_reason"] == "Only active-active"


def test_offline_rows_have_no_gauge_readings_and_counters_stay():
    aps = _groups()["access_points"]
    offline = next(r for r in aps["rows"] if r["label"] == "AP-off-0")
    assert offline["absent"] is True
    assert offline["cells"]["fgt_ap_cpu"]["value"] is None
    assert offline["cells"]["fgt_ap_cpu"]["suppressed"] == di.SUPPRESSED_OFFLINE
    assert offline["cells"]["fgt_ap_clients"]["value"] is None
    assert offline["cells"]["fgt_ap_status"]["status"] == "crit"
    online = next(r for r in aps["rows"] if r["label"] == "AP-on-0")
    assert online["cells"]["fgt_ap_cpu"]["value"] == 12 and "absent" not in online
    tunnel = next(r for r in _groups()["ipsec"]["rows"] if r["label"] == "ACC_To_Amman")
    assert tunnel["cells"]["fgt_tun_in_bps"]["value"] == 0  # a rate counter of a down tunnel is real


def test_a_zero_capacity_is_not_reported():
    wifi = {m["key"]: m for m in _groups()["wifi"]["metrics"]}
    assert wifi["fgt_wifi_capacity"]["has_data"] is False and wifi["fgt_wifi_capacity"]["value"] is None
    assert wifi["fgt_wifi_clients"]["value"] == 136


def test_group_digests_state_values_not_row_counts():
    g = _groups()
    assert di.group_digest(g["access_points"])["summary"] == "9/13 online"
    assert di.group_digest(g["ipsec"])["summary"] == "1/2 up"
    assert di.group_digest(g["system"])["summary"] == "CPU Usage 13% · Memory Usage 54%"
    assert di.group_digest(g["ha_members"])["status"] == "na"
    one_row = {"key": "cpu", "name": "CPU", "kind": "table", "status": "ok",
               "columns": [{"key": "c", "name": "CPU", "unit": "%", "type": "gauge"}],
               "rows": [{"instance": "1", "label": "1", "cells": {"c": {"value": 45, "status": "ok"}}}]}
    assert di.group_digest(one_row)["summary"] == "1 entry · max 45%"


def test_template_issues_name_rows_or_count_them():
    issues = di.template_issues(list(_groups().values()), NOW)
    titles = [i["title"] for i in issues]
    assert "IPsec Tunnels: ACC_To_Amman Down" in titles
    ap = next(i for i in issues if i["group"] == "access_points")
    assert ap["title"] == "FortiAP Access Points: 4 of 13 Offline" and ap["detail"] == "AP-off-0, AP-off-1, +2"
    assert all(i["severity"] == "critical" for i in issues)
    rollup = di.health_rollup(issues)
    assert rollup == {"state": "critical", "issue_count": 2, "critical": 2, "warning": 0}


def test_issue_order_is_severity_then_age():
    issues = di.sort_issues([
        {"severity": "warning", "since": "2026-10-18T10:00:00+00:00"},
        {"severity": "critical", "since": None},
        {"severity": "critical", "since": "2026-10-17T10:00:00+00:00"},
        {"severity": "info", "since": "2026-10-01T10:00:00+00:00"},
    ])
    assert [(i["severity"], i["since"]) for i in issues] == [
        ("critical", "2026-10-17T10:00:00+00:00"), ("critical", None),
        ("warning", "2026-10-18T10:00:00+00:00"), ("info", "2026-10-01T10:00:00+00:00")]


def test_interface_breakdown_counts_only_unexpected_down_as_actionable():
    rows = [
        {"if_index": 1, "if_name": "Gi1/0/49", "oper_status": "up", "admin_status": "up", "monitored": True, "ever_up": True},
        {"if_index": 2, "if_name": "Gi1/0/1", "oper_status": "down", "admin_status": "up", "monitored": True, "ever_up": False},
        {"if_index": 3, "if_name": "Gi1/0/2", "oper_status": "down", "admin_status": "down", "monitored": True, "ever_up": True},
        {"if_index": 4, "if_name": "StackPort1", "oper_status": "notPresent", "admin_status": "up", "monitored": True, "ever_up": False},
        {"if_index": 5, "if_name": "Gi1/0/50", "oper_status": "down", "admin_status": "up", "monitored": True, "ever_up": True},
        {"if_index": 6, "if_name": "Gi1/0/3", "oper_status": "lowerLayerDown", "admin_status": "up", "monitored": False, "ever_up": True},
    ]
    out = di.interface_breakdown(rows)
    assert (out["total"], out["up"], out["unexpected_down"], out["admin_down"], out["not_present"],
            out["unused"], out["unmonitored_down"]) == (6, 1, 1, 1, 1, 1, 1)
    assert out["unexpected"] == [{"if_index": 5, "name": "Gi1/0/50", "alias": None}]


def test_identity_has_one_answer_with_its_source():
    groups = list(_groups().values())
    out = di.resolve_identity({"model": "FGT_101F", "os_version": None, "firmware_version": None, "serial_number": None},
                              groups, {"serial_number": "FG101FTK23015380", "model_name": "FGT_101F", "fw_revision": None})
    assert out["os_version"] == {"value": "7.6.7 build 3704", "source": "template"}
    assert out["firmware"] == {"value": "7.6.7 build 3704", "source": "template"}
    assert out["serial_number"] == {"value": "FG101FTK23015380", "source": "entity"}
    assert out["model"] == {"value": "FGT_101F", "source": "device"}
    normalized = {"os_product": "FortiOS", "os_version": "7.6.7", "os_build": "3704", "hw_model": "FortiGate-101F", "hw_serial": None}
    out = di.resolve_identity({"model": "FGT_101F"}, groups, None, normalized)
    assert out["os_version"] == {"value": "FortiOS 7.6.7 build 3704", "source": "inventory"}
    assert out["model"]["source"] == "inventory"
    assert di.resolve_identity({}, [], None)["serial_number"] == {"value": None, "source": None}


# ── GET /devices/{id}/summary ──────────────────────────────────────────────

class _Result:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class _DB:
    def __init__(self, alerts=()):
        self.alerts = list(alerts)

    async def execute(self, sql, params=None):
        q = str(sql)
        if "FROM device_profiles" in q:
            return _Result([{"name": "Fortinet FortiGate", "vendor": "Fortinet", "oid_groups": FORTIGATE}])
        if "FROM device_template_values" in q:
            return _Result(_fortigate_values())
        if "FROM device_entities" in q:
            return _Result([{"model_name": "FGT_101F", "serial_number": "FG101FTK23015380", "fw_revision": None}])
        if "FROM inventory_identity" in q:
            return _Result([])
        if "FROM device_interfaces" in q:
            return _Result([
                {"if_index": 1, "if_name": "wan2", "if_descr": "wan2", "if_alias": "ITC_DIA", "if_type": 6,
                 "if_speed": 100_000_000, "configured_speed_bps": None, "admin_status": "up", "oper_status": "up",
                 "monitored": True, "ever_up": True},
                {"if_index": 2, "if_name": "port1", "if_descr": "port1", "if_alias": "New_ACC_LAN", "if_type": 6,
                 "if_speed": 1_000_000_000, "configured_speed_bps": None, "admin_status": "up", "oper_status": "up",
                 "monitored": True, "ever_up": True},
                {"if_index": 3, "if_name": "port9", "if_descr": "port9", "if_alias": None, "if_type": 6,
                 "if_speed": 1_000_000_000, "configured_speed_bps": None, "admin_status": "up", "oper_status": "down",
                 "monitored": True, "ever_up": False},
            ])
        if "FROM alerts" in q:
            return _Result(self.alerts)
        if "FROM device_maintenance" in q:
            return _Result([])
        if "FROM device_flap_episodes" in q:
            return _Result([])
        raise AssertionError(f"unexpected query: {q}")

    async def rollback(self):
        pass


def _device(**over):
    d = dict(id=uuid4(), status="up", status_changed_at=NOW - timedelta(days=2, hours=21), profile_id=uuid4(),
             model="FGT_101F", os_version=None, firmware_version=None, serial_number=None, snmp_enabled=True,
             ping_enabled=True, ping_interval=60, snmp_poll_interval=60, last_seen=NOW, is_flapping=False,
             flapping_since=None, tags=[])
    d.update(over)
    return NS(**d)


@pytest.fixture
def summary_env(monkeypatch):
    state = {"device": _device(), "can_alerts": True, "rates_fail": False}

    async def get_device(db, device_id):
        return state["device"]

    async def has_permission(db, user, perm):
        return state["can_alerts"] if perm == "alerts.view" else True

    def rates(device_id):
        if state["rates_fail"]:
            raise RuntimeError("clickhouse down")
        return {1: {"in_bps": 24e6, "out_bps": 5.6e6, "ts": 1}, 2: {"in_bps": 17.3e6, "out_bps": 99.5e6, "ts": 1}}

    monkeypatch.setattr(devices_api.device_service, "get_device", get_device)
    monkeypatch.setattr("app.core.security.user_has_permission", has_permission)
    monkeypatch.setattr(devices_api, "_latest_interface_rates", rates)
    monkeypatch.setattr(devices_api, "_recent_traps", lambda device_id, limit=5: [])
    monkeypatch.setattr(devices_api.metric_service, "get_status_history", lambda *a, **k: [])
    return state


@pytest.mark.asyncio
async def test_summary_keeps_status_and_reports_issues_separately(summary_env):
    out = await devices_api.get_device_page_summary(uuid4(), _DB(), NS(id=uuid4()))
    assert out["status"] == "up"  # never rewritten by the rollup
    assert out["health"]["state"] == "critical" and out["health"]["issue_count"] == 2
    assert [i["title"] for i in out["issues"]] == ["IPsec Tunnels: ACC_To_Amman Down", "FortiAP Access Points: 4 of 13 Offline"]
    assert out["not_applicable"][0]["key"] == "ha_members"
    assert out["identity"]["os_version"]["value"] == "7.6.7 build 3704"
    assert out["interfaces"]["unexpected_down"] == 0 and out["interfaces"]["unused"] == 1
    assert [i["name"] for i in out["busiest_interfaces"]] == ["wan2", "port1"]  # WAN first
    assert out["busiest_interfaces"][0]["util_pct"] == 24.0
    assert out["alerts"] == {"active": 0, "acknowledged": 0, "items": []}
    assert out["unavailable"] == []


@pytest.mark.asyncio
async def test_summary_omits_alerts_without_permission_and_names_missing_sources(summary_env):
    summary_env["can_alerts"] = False
    summary_env["rates_fail"] = True
    out = await devices_api.get_device_page_summary(uuid4(), _DB(), NS(id=uuid4()))
    assert out["alerts"] is None
    assert out["busiest_interfaces"] is None and "interface_rates" in out["unavailable"]


@pytest.mark.asyncio
async def test_summary_puts_a_confirmed_down_and_open_alerts_first(summary_env):
    summary_env["device"] = _device(status="down")
    alerts = [{"id": uuid4(), "severity": "critical", "status": "active",
               "message": "[ZenPlus CRITICAL] Device unreachable", "triggered_at": NOW - timedelta(minutes=5)}]
    out = await devices_api.get_device_page_summary(uuid4(), _DB(alerts), NS(id=uuid4()))
    assert out["status"] == "down"
    assert out["issues"][0]["source"] == "status" and out["issues"][0]["severity"] == "critical"
    assert any(i["source"] == "alert" and i["title"] == "Device unreachable" for i in out["issues"])
    assert out["alerts"]["active"] == 1


@pytest.mark.asyncio
async def test_summary_404_for_unknown_device(summary_env):
    summary_env["device"] = None
    with pytest.raises(devices_api.HTTPException) as exc:
        await devices_api.get_device_page_summary(uuid4(), _DB(), NS(id=uuid4()))
    assert exc.value.status_code == 404


def test_template_kpis_pick_thresholds_and_vendor_highlights():
    tpl = [dict(g) for g in FORTIGATE]
    system = next(g for g in tpl if g["key"] == "system")
    system["metrics"] = system["metrics"] + [
        {"key": "fgt_sessions", "name": "Active Sessions", "type": "gauge", "unit": "sessions"},
        {"key": "fgt_ses_rate", "name": "Session Setup Rate (1m)", "type": "gauge", "unit": "sessions/s"}]
    vals = _fortigate_values() + [_v("system", "fgt_sessions", 16873), _v("system", "fgt_ses_rate", 136)]
    groups, _ = di.shape_template_groups(tpl, vals)
    out = di.template_kpis(groups)
    assert out["cpu_thresholds"] == {"warn": 80, "crit": 90}
    assert out["memory_thresholds"] == {"warn": 80, "crit": 88}
    assert out["highlights"][0] == {"kind": "sessions", "label": "Sessions", "value": 16873, "unit": "sessions",
                                    "sub": "136 sessions/s setup rate", "series_key": "tpl_fgt_sessions_"}
    assert out["highlights"][1]["kind"] == "clients"
    poe = [{"key": "poe", "name": "PoE Budget", "kind": "table", "metrics": [
        {"key": "b", "name": "Budget", "type": "gauge", "unit": "W"},
        {"key": "c", "name": "Consumed", "type": "gauge", "unit": "W"}]}]
    groups, _ = di.shape_template_groups(poe, [_v("poe", "b", 740, instance="1"), _v("poe", "c", 54, instance="1")])
    assert di.template_kpis(groups)["highlights"] == [
        {"kind": "poe", "label": "PoE power", "value": 54, "unit": "W", "sub": "of 740 W budget (7%)", "series_key": None}]


def test_uptime_stats_can_be_narrowed_to_one_device(client, as_admin, monkeypatch):
    """The device page reads its headline with the same calculation, one device only."""
    from app.core import database
    calls = []

    def query(sql, parameters=None):
        calls.append((sql, parameters))
        return NS(result_rows=[("dev-1", 99.0, 100.0)])
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: NS(query=query))
    device = str(uuid4())
    r = client.get("/api/v1/devices/dashboard/uptime-stats",
                   params={"from": (NOW - timedelta(hours=1)).isoformat(), "to": NOW.isoformat(), "device_id": device})
    assert r.status_code == 200
    sql, params = calls[0]
    assert "toString(device_id) = %(device)s" in sql and params["device"] == device
    assert r.json()["devices"] == {"dev-1": 99.0}
    r = client.get("/api/v1/devices/dashboard/uptime-stats",
                   params={"from": (NOW - timedelta(hours=1)).isoformat(), "to": NOW.isoformat()})
    assert "device" not in calls[-1][1]
