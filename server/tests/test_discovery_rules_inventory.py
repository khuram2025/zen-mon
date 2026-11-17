"""Discovery rules phase 2, pure parts: the change planner for existing assets.

``plan_changes`` decides what a rule's plan changes on a device or server
given its current values and the assignments rules made before; preview,
apply and the "when added" sweep all go through it.
"""
from __future__ import annotations

from app.services.discovery_rules import actions, conditions, engine
from app.services.discovery_rules.facts import facts_from_device, facts_from_server
from app.services.discovery_rules.inventory import current_values, digest, plan_changes, summarise

G1, G2 = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2"
DEV = "11111111-1111-1111-1111-111111111111"


def rule(rid, prio, acts, cond=None, *, enforce=False, kinds=("device", "server")):
    return {"id": rid, "name": rid, "priority": prio, "revision": 3, "mode": "active", "asset_kinds": list(kinds),
            "triggers": ["manual"], "profile_ids": None,
            "condition": conditions.validate(cond or {"op": "all", "conditions": []}),
            "actions": actions.validate_actions(acts, list(kinds)), "stop_processing": False,
            "enforcement": "enforce" if enforce else "set_once"}


def device(**kw):
    row = {"id": DEV, "hostname": "fw1", "ip_address": "10.51.2.12", "device_type": "other", "group_id": None,
           "profile_id": None, "location": None, "owner": None, "tags": ["Mine"], "ping_interval": 60,
           "snmp_poll_interval": 60, "ping_enabled": True, "snmp_enabled": False, "snmp_credential_id": None,
           "vendor": "Fortinet", "sys_object_id": ".1.3.6.1.4.1.12356.101.1", "status": "up"}
    row.update(kw)
    return row


def run(row, rules, owned=None, kind="device"):
    facts = facts_from_server(row) if kind == "server" else facts_from_device(row)
    plan = engine.evaluate(facts, rules, trigger="manual", kind=kind)
    return plan_changes(kind, row["id"], current_values(kind, row), plan, owned or {}, {r["id"]: r for r in rules})


def test_new_values_are_changed_and_equal_ones_are_not():
    rules = [rule("r", 10, [{"key": "group", "value": G1}, {"key": "location", "value": "Riyadh DC"},
                            {"key": "ping_interval", "value": 60}, {"key": "tags", "value": ["firewall", "mine"]}])]
    ch = run(device(), rules)
    assert [(c["action_key"], c["item_key"], c["before"], c["after"], c["outcome"]) for c in ch] == [
        ("group", "", None, G1, "apply"), ("location", "", None, "Riyadh DC", "apply"),
        ("tags", "firewall", None, "firewall", "apply")]          # ping 60 = 60; "mine" already there
    assert all(c["rule_revision"] == 3 for c in ch)


def test_a_hand_edit_is_kept_by_set_once_and_restored_by_enforce():
    owned = {("group", ""): {"value": G1}}
    edited = device(group_id=G2)                                   # the rule set G1, someone chose G2
    keep = run(edited, [rule("r", 10, [{"key": "group", "value": G1}])], owned)
    assert [(c["outcome"], c["before"], c["after"]) for c in keep] == [("kept_manual_edit", G2, G1)]
    back = run(edited, [rule("r", 10, [{"key": "group", "value": G1}], enforce=True)], owned)
    assert [(c["outcome"], c["before"], c["after"]) for c in back] == [("apply", G2, G1)]
    assert "enforce" in back[0]["detail"]
    # unchanged since the rule set it: nothing to do
    assert run(device(group_id=G1), [rule("r", 10, [{"key": "group", "value": G1}])], owned) == []


def test_a_rule_owned_value_follows_the_rule_when_the_rule_changes():
    owned = {("group", ""): {"value": G1}}
    ch = run(device(group_id=G1), [rule("r", 10, [{"key": "group", "value": G2}])], owned)
    assert [(c["outcome"], c["before"], c["after"]) for c in ch] == [("apply", G1, G2)]


def test_a_removed_rule_tag_is_a_hand_edit():
    owned = {("tags", "firewall"): {"value": "firewall"}}
    rules = [rule("r", 10, [{"key": "tags", "value": ["firewall"]}])]
    assert [c["outcome"] for c in run(device(tags=[]), rules, owned)] == ["kept_manual_edit"]
    rules[0]["enforcement"] = "enforce"
    assert [c["outcome"] for c in run(device(tags=[]), rules, owned)] == ["apply"]


def test_a_credential_never_replaces_a_devices_own_snmp_settings():
    cred = "00000000-0000-0000-0000-0000000000c1"
    rules = [rule("r", 10, [{"key": "snmp_credential", "value": cred}])]
    assert [c["outcome"] for c in run(device(snmp_enabled=True), rules)] == ["not_reversible"]
    assert [c["outcome"] for c in run(device(snmp_enabled=False), rules)] == ["apply"]
    other = "00000000-0000-0000-0000-0000000000c2"
    assert [c["outcome"] for c in run(device(snmp_enabled=True, snmp_credential_id=other), rules)] == ["apply"]


def test_the_import_decision_never_touches_inventory_and_servers_take_server_settings():
    rules = [rule("r", 10, [{"key": "decision", "value": "ignore"}, {"key": "owner", "value": "NOC"},
                            {"key": "environment", "value": "production"}, {"key": "tags", "value": ["dc1"]}],
                  kinds=("server",))]
    srv = {"id": DEV, "display_name": "app01", "hostname": "app01", "primary_ip": "10.1.1.1", "os_type": "linux",
           "environment": None, "owner": None, "tags": [], "status": "healthy"}
    ch = run(srv, rules, kind="server")
    assert {c["action_key"] for c in ch} == {"owner", "environment", "tags"}
    assert run(device(), rules) == []                              # a server-only rule skips devices


def test_ping_is_only_ever_turned_on():
    rules = [rule("r", 10, [{"key": "ping_enabled", "value": True}], kinds=("device",))]
    assert [(c["before"], c["after"]) for c in run(device(ping_enabled=False), rules)] == [(False, True)]
    assert run(device(ping_enabled=True), rules) == []


def test_first_matching_rule_wins_on_inventory_too():
    rules = [rule("low", 20, [{"key": "group", "value": G2}]), rule("high", 10, [{"key": "group", "value": G1}])]
    ch = run(device(), rules)
    assert [(c["after"], c["rule_id"]) for c in ch] == [(G1, "high")]


def test_digest_and_summary():
    rules = [rule("r", 10, [{"key": "group", "value": G1}, {"key": "tags", "value": ["a"]}])]
    ch = run(device(), rules)
    assert digest(ch) == digest(list(reversed(ch)))
    assert digest(ch) != digest(ch[:1])
    s = summarise(ch, assets=5, matched=1)
    assert (s["changes"], s["assets_changed"], s["by_action"]) == (2, 1, {"group": 1, "tags": 1})


def test_inventory_facts():
    f = facts_from_device(device(tags=["Riyadh"], group_id=G1))
    assert f["sys_object_id"] == "1.3.6.1.4.1.12356.101.1" and f["group_id"] == G1 and f["tags"] == ["Riyadh"]
    assert f["is_server"] is False and f["open_ports"] is None
    assert conditions.evaluate(conditions.validate(
        {"field": "tags", "operator": "has_any", "value": ["riyadh"]}), f)
    s = facts_from_server({"id": DEV, "display_name": "app01", "primary_ip": "10.1.1.1", "os_name": "Ubuntu",
                           "os_type": "linux", "environment": "prod", "tags": '["web"]'})
    assert s["is_server"] and s["hostname"] == "app01" and s["tags"] == ["web"] and s["environment"] == "prod"


def test_server_permissions_follow_the_rule_kinds():
    acts = actions.validate_actions([{"key": "tags", "value": ["x"]}, {"key": "owner", "value": "NOC"}],
                                    ["device", "server"])
    assert actions.permissions_needed(acts, ["device"]) == {"devices.manage"}
    assert actions.permissions_needed(acts, ["server"]) == {"servers.manage"}
    assert actions.permissions_needed(acts, ["device", "server"]) == {"devices.manage", "servers.manage"}
