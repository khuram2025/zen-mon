"""State rules on device-template enum metrics (alerting plan G-3), without a database.

Covers: which codes of a value map are bad and how that is written as ordinary
numeric conditions; the wording of a state incident (component, the template's
metric name, the value label; never the series key or the code); rule-pack
state recommendations and the controller-managed-children exclusion; the
refusals for retired and unmapped metric keys; per-sensor temperature series.
"""

import pytest
from fastapi import HTTPException

from app.services import rule_packs
from app.services import template_states as ts
from app.services.network_conditions import (RETIRED_METRICS, UNMAPPED_METRICS, evaluate,
                                             refuse_new_metrics, validate_conditions)
from app.services.network_history import entities

CISCO_STATES = {"1": {"text": "Normal", "sev": "ok"}, "2": {"text": "Warning", "sev": "warn"},
                "3": {"text": "Critical", "sev": "crit"}, "4": {"text": "Shutdown", "sev": "crit"},
                "5": {"text": "Not Present", "sev": "info"}, "6": {"text": "Not Functioning", "sev": "crit"}}

FORTIGATE = [
    {"key": "system", "name": "System Health", "kind": "scalar", "metrics": [
        {"key": "fgt_cpu", "name": "CPU Usage", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}}]},
    {"key": "ha_members", "name": "HA Members", "kind": "table", "metrics": [
        {"key": "fgt_ha_sync", "name": "Sync Status", "type": "enum",
         "labels": {"0": {"text": "Not Synchronized", "sev": "crit"}, "1": {"text": "Synchronized", "sev": "ok"}}}]},
    {"key": "ipsec", "name": "IPsec Tunnels", "kind": "table", "metrics": [
        {"key": "fgt_tun_status", "name": "Status", "type": "enum",
         "labels": {"1": {"text": "Down", "sev": "crit"}, "2": {"text": "Up", "sev": "ok"}}}]},
    # Controller-managed children: rows are FortiAPs / FortiSwitches.
    {"key": "access_points", "name": "FortiAP Access Points", "kind": "table",
     "children": {"device_type": "access_point", "cpu_key": "fgt_ap_cpu"}, "metrics": [
        {"key": "fgt_ap_status", "name": "Status", "type": "enum",
         "labels": {"1": {"text": "Offline", "sev": "crit"}, "2": {"text": "Online", "sev": "ok"}}},
        {"key": "fgt_ap_cpu", "name": "CPU", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}},
        {"key": "fgt_ap_mem", "name": "Memory", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}}]},
    {"key": "fortiswitch", "name": "Managed FortiSwitch", "kind": "table",
     "children": {"device_type": "switch"}, "metrics": [
        {"key": "fgt_sw_cpu", "name": "CPU", "type": "gauge", "unit": "%", "thresholds": {"warn": 80, "crit": 90}}]},
    {"key": "fap_radios", "name": "FortiAP Radios", "kind": "table", "display": "children", "metrics": [
        {"key": "fgt_radio_util", "name": "Channel use", "type": "gauge", "unit": "%", "thresholds": {"crit": 90}}]},
    {"key": "fsw_ports", "name": "FortiSwitch Ports", "kind": "table",
     "belongs_to": {"group": "fortiswitch", "by": "serial"}, "metrics": [
        {"key": "fgt_swp_state", "name": "State", "type": "enum",
         "labels": {"1": {"text": "Down", "sev": "crit"}, "2": {"text": "Up", "sev": "ok"}}}]},
]
CISCO = [
    {"key": "psu", "name": "Power Supplies", "kind": "table", "metrics": [
        {"key": "cisco_psu_state", "name": "State", "type": "enum", "labels": CISCO_STATES}]},
    {"key": "fans", "name": "Fans", "kind": "table", "metrics": [
        {"key": "cisco_fan_state", "name": "State", "type": "enum", "labels": CISCO_STATES}]},
]
JUNIPER = [
    {"key": "alarms", "name": "Chassis Alarms", "kind": "scalar", "metrics": [
        {"key": "jnx_alarm_yellow", "name": "Yellow Alarm", "type": "enum",
         "labels": {"1": {"text": "Other", "sev": "info"}, "2": {"text": "Off", "sev": "ok"}, "3": {"text": "On", "sev": "warn"}}},
        {"key": "jnx_alarm_red", "name": "Red Alarm", "type": "enum",
         "labels": {"1": {"text": "Other", "sev": "info"}, "2": {"text": "Off", "sev": "ok"}, "3": {"text": "On", "sev": "crit"}}}]},
    {"key": "bgp_peers", "name": "BGP Peers", "kind": "table", "metrics": [
        {"key": "jnx_bgp_state", "name": "State", "type": "enum", "labels": {
            "1": {"text": "Idle", "sev": "warn"}, "2": {"text": "Connect", "sev": "warn"},
            "3": {"text": "Active", "sev": "warn"}, "4": {"text": "OpenSent", "sev": "warn"},
            "5": {"text": "OpenConfirm", "sev": "warn"}, "6": {"text": "Established", "sev": "ok"}}}]},
]


def _breaches(conds, code):
    return evaluate(conds, "AND", {conds[0]["metric"]: [(0, code)]}, now=1).breach


# ── Bad states as numeric conditions ────────────────────────────────────────

def test_bad_codes_come_from_the_value_map():
    idx = ts.metric_index(FORTIGATE + CISCO + JUNIPER)
    assert ts.bad_codes(idx["fgt_ha_sync"]) == [0]
    assert ts.bad_codes(idx["cisco_psu_state"]) == [3, 4, 6]       # not Warning, not Not Present
    assert ts.bad_codes(idx["jnx_bgp_state"]) == [1, 2, 3, 4, 5]   # everything but Established
    assert ts.bad_codes(idx["jnx_alarm_yellow"]) == []              # warn beside info: ambiguous
    assert ts.bad_codes(idx["fgt_cpu"]) == []                       # a gauge has no states


@pytest.mark.parametrize("key,codes", [
    ("cisco_psu_state", {1: False, 2: False, 3: True, 4: True, 5: False, 6: True}),
    ("jnx_bgp_state", {1: True, 5: True, 6: False}),
    ("fgt_ha_sync", {0: True, 1: False}),
])
def test_conditions_breach_exactly_on_the_bad_states(key, codes):
    info = ts.metric_index(FORTIGATE + CISCO + JUNIPER)[key]
    conds = ts.conditions_for(f"tpl_{key}", info, ts.bad_codes(info))
    validate_conditions(conds)
    assert len(conds) <= ts.MAX_CONDITIONS
    assert {code: _breaches(conds, code) for code in codes} == codes


def test_cisco_power_supply_conditions_skip_an_empty_bay():
    info = ts.metric_index(CISCO)["cisco_psu_state"]
    assert ts.conditions_for("tpl_cisco_psu_state", info, ts.bad_codes(info)) == [
        {"metric": "tpl_cisco_psu_state", "operator": ">=", "threshold": 3.0},
        {"metric": "tpl_cisco_psu_state", "operator": "!=", "threshold": 5.0}]


# ── Wording ─────────────────────────────────────────────────────────────────

def test_state_sentence_names_component_metric_and_value_label():
    idx = ts.metric_index(FORTIGATE + CISCO)
    tunnel = ts.component_label(idx["fgt_tun_status"], "Branch-12")
    out = ts.describe(idx["fgt_tun_status"], 1.0, component=tunnel, who="Firewall FGT-01 (10.0.0.1)")
    assert out["sentence"] == "IPsec tunnel Branch-12 on Firewall FGT-01 (10.0.0.1) is Down."
    back = ts.describe(idx["fgt_tun_status"], 2.0, component=tunnel, who="Firewall FGT-01 (10.0.0.1)",
                       recovered=True)
    assert back["sentence"] == "IPsec tunnel Branch-12 on Firewall FGT-01 (10.0.0.1) is Up again."
    psu = ts.describe(idx["cisco_psu_state"], 3, component=ts.component_label(idx["cisco_psu_state"], "PS2"),
                      who="Switch core-1 (10.0.0.2)")
    assert psu["sentence"] == "Power supply PS2 on Switch core-1 (10.0.0.2) is Critical."
    sync = ts.describe(idx["fgt_ha_sync"], 0, component=ts.component_label(idx["fgt_ha_sync"], "FGT-B"),
                       who="Firewall FGT-01")
    assert sync["sentence"] == "HA member FGT-B on Firewall FGT-01: Sync Status is Not Synchronized."
    for text in (out["sentence"], psu["sentence"], sync["sentence"]):
        assert "tpl_" not in text and "code" not in text


def test_unknown_code_and_scalar_metric_wording():
    idx = ts.metric_index(JUNIPER + FORTIGATE)
    red = ts.describe(idx["jnx_alarm_red"], 3, component=None, who="Router mx-1 (10.0.0.3)")
    assert red["sentence"] == "Red Alarm on Router mx-1 (10.0.0.3) is On."
    odd = ts.describe(idx["fgt_tun_status"], 7, component="IPsec tunnel X", who="Firewall a")
    assert odd["value_label"] == "an unknown state (code 7)"


@pytest.mark.parametrize("group,noun", [("IPsec Tunnels", "IPsec tunnel"), ("Power Supplies", "Power supply"),
                                        ("HA Members", "HA member"), ("BGP Peers", "BGP peer"),
                                        ("Fans", "Fan"), ("Pool members", "Pool member")])
def test_component_noun(group, noun):
    assert ts.component_noun(group) == noun


def test_component_label_does_not_repeat_the_noun():
    info = ts.metric_index(CISCO)["cisco_fan_state"]
    assert ts.component_label(info, "Fan 2") == "Fan 2"
    assert ts.component_label(info, "Switch 1 - FAN 1") == "Fan Switch 1 - FAN 1"
    assert ts.component_label(info, "") is None


# ── Rule packs ──────────────────────────────────────────────────────────────

def test_state_recommendations_severity_priority_and_hold():
    by = {i["key"]: i for i in rule_packs.recommendations(FORTIGATE + CISCO + JUNIPER, None)}
    expect = {"fgt_ha_sync:state": ("critical", "P2"), "fgt_tun_status:state": ("warning", "P3"),
              "cisco_psu_state:state": ("critical", "P2"), "cisco_fan_state:state": ("warning", "P3"),
              "jnx_bgp_state:state": ("critical", "P2"), "jnx_alarm_red:state": ("critical", "P2")}
    for key, (sev, prio) in expect.items():
        it = by[key]
        assert (it["severity"], it["priority"]) == (sev, prio), key
        assert it["min_duration"] == 120 and it["source"] == "state"
    assert "jnx_alarm_yellow:state" not in by
    assert by["jnx_bgp_state:state"]["name"] == "BGP Peers: State is not Established"
    assert by["fgt_tun_status:state"]["name"] == "IPsec Tunnels: Status is Down"
    assert by["cisco_psu_state:state"]["states"] == [3, 4, 6]


def test_no_recommendations_for_controller_managed_children():
    pack = [{"key": "ap_cpu", "name": "AP CPU", "metric": "tpl_fgt_ap_cpu", "operator": ">", "threshold": 90},
            {"key": "uplink_down", "name": "Uplink down", "metric": "if_oper_status", "operator": "==",
             "threshold": 2, "if_role": "uplink"}]
    items = rule_packs.recommendations(FORTIGATE, rule_packs.validate_pack(pack))
    metrics = {i["metric"] for i in items}
    for child in ("tpl_fgt_ap_cpu", "tpl_fgt_ap_mem", "tpl_fgt_sw_cpu", "tpl_fgt_ap_status",
                  "tpl_fgt_radio_util", "tpl_fgt_swp_state"):
        assert child not in metrics, child
    assert {"tpl_fgt_cpu", "if_oper_status", "tpl_fgt_tun_status"} <= metrics


def test_accepting_a_state_recommendation_writes_all_its_conditions():
    item = {i["key"]: i for i in rule_packs.recommendations(CISCO, None)}["cisco_psu_state:state"]
    f = rule_packs.rule_fields(item)
    assert f["conditions"] == item["conditions"] and len(f["conditions"]) == 2
    assert (f["metric"], f["operator"], f["threshold"]) == ("tpl_cisco_psu_state", ">=", 3.0)
    assert (f["min_duration"], f["severity"], f["priority"]) == (120, "critical", "P2")


def test_changed_bad_states_show_as_a_change_and_old_records_compare_unchanged():
    item = {i["key"]: i for i in rule_packs.recommendations(CISCO, None)}["cisco_psu_state:state"]
    accepted = rule_packs.values(item)
    assert rule_packs.changes(accepted, item) == []
    assert rule_packs.changes(accepted, {**item, "states": [3, 4]}) == [
        {"field": "states", "from": [3, 4, 6], "to": [3, 4]}]
    gauge = {i["key"]: i for i in rule_packs.recommendations(FORTIGATE, None)}["fgt_cpu:crit"]
    old_record = {f: gauge.get(f) for f in rule_packs.VALUE_FIELDS}       # accepted before 1.25.18
    assert rule_packs.changes(old_record, gauge) == []


# ── Retired and unmapped keys ───────────────────────────────────────────────

@pytest.mark.parametrize("metric,needle", [("fan_state", "tpl_cisco_fan_state"), ("psu_state", "tpl_cisco_psu_state"),
                                           ("vpn_tunnel_state", "tpl_fgt_tun_status"), ("ha_state", "tpl_fgt_ha_sync"),
                                           ("bgp_neighbor_down", "tpl_jnx_bgp_state")])
def test_unmapped_keys_name_the_template_state_metric(metric, needle):
    from app.api.v1.alert_rules import _validate_network_contract
    assert set(UNMAPPED_METRICS) == {"fan_state", "psu_state", "vpn_tunnel_state", "ha_state", "bgp_neighbor_down"}
    with pytest.raises(HTTPException) as exc:
        _validate_network_contract(metric, None)
    assert exc.value.status_code == 422 and needle in exc.value.detail
    # Even an old rule cannot keep one: there is nothing to evaluate.
    with pytest.raises(HTTPException):
        _validate_network_contract(metric, None, existing={"metric": metric})


@pytest.mark.parametrize("metric,needle", [("jitter", "do not measure jitter"),
                                           ("session_count", "tpl_fgt_sessions")])
def test_retired_keys_refused_for_new_rules_but_old_rules_stay_editable(metric, needle):
    from app.api.v1.alert_rules import _validate_network_contract
    with pytest.raises(HTTPException) as exc:
        _validate_network_contract(metric, None)
    assert exc.value.status_code == 422 and needle in exc.value.detail
    _validate_network_contract(metric, None, existing={"metric": metric})
    _validate_network_contract(metric, [dict(metric=metric, operator=">", threshold=5)],
                               existing={"metric": "cpu", "conditions": [{"metric": metric}]})
    with pytest.raises(ValueError):
        refuse_new_metrics([dict(metric=metric)], existing={"cpu"})
    assert metric in RETIRED_METRICS


def test_temperature_is_still_accepted():
    from app.api.v1.alert_rules import _validate_network_contract
    _validate_network_contract("temperature", None)


# ── Per-sensor temperature ──────────────────────────────────────────────────

def test_temperature_rule_is_one_dimension_per_sensor():
    conds = [dict(metric="temperature", operator=">", threshold=70)]
    series = {"cpu": [(0, 5)], "temperature_5": [(0, 41.0)], "temperature_6": [(0, 78.0)]}
    got = {identity: (extra["series_suffix"], evaluate(conds, "AND", h, now=1).breach)
           for identity, _, h, extra in entities(conds, series, {}, [], lambda i: True)}
    assert got == {"series:temperature_5": ("_5", False), "series:temperature_6": ("_6", True)}
    # A device without sensor series yields nothing to evaluate (never a recovery).
    assert list(entities(conds, {"cpu": [(0, 5)]}, {}, [], lambda i: True)) == []
    with pytest.raises(ValueError):
        validate_conditions(conds + [dict(metric="if_oper_status", operator="==", threshold=2)])
