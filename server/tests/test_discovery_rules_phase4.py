"""Discovery rules phase 4, the pure parts: interface patterns, interface item
keys, the interface planner, automatic-run limits and the schedule."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.discovery_rules import actions, engine, inventory, reconcile


def iface(idx, name, **kw):
    return {"if_index": idx, "if_name": name, "monitored": kw.pop("monitored", True), **kw}


def test_interface_patterns_match_text_types_and_speed():
    m = actions.interface_matches
    assert m({"field": "if_name", "pattern": "^gi"}, iface(1, "GigabitEthernet1/0/1"))
    assert not m({"field": "if_alias", "pattern": "uplink"}, iface(1, "Gi1", if_alias=None))
    assert m({"field": "if_type", "pattern": "24,53"}, iface(1, "Lo0", if_type=24))
    assert not m({"field": "if_type", "pattern": "24,53"}, iface(1, "Gi1", if_type=6))
    ten_g = iface(1, "Te1", if_speed=10_000_000_000)
    assert m({"field": "speed", "pattern": "10000-"}, ten_g)
    assert not m({"field": "speed", "pattern": "-1000"}, ten_g)
    assert m({"field": "speed", "pattern": "100"}, iface(1, "Fa0", if_speed=10_000_000, configured_speed_bps=100_000_000))
    assert not m({"field": "speed", "pattern": "1-"}, iface(1, "x"))          # unknown speed never matches


def test_interface_items_follow_the_name_unless_shared_or_empty():
    rows = inventory.iface_rows([iface(1, "Gi1/0/1"), iface(2, "Vlan1"), iface(3, "vlan1"), iface(4, "")])
    assert sorted(rows) == ["#2", "#3", "#4", "gi1/0/1"]
    assert inventory.iface_rows(None) == {}


def _plan(*entries):
    return {"settings": {}, "tags": [], "matched": [{"rule_id": e[0]} for e in entries],
            "additive": {"interfaces": [{"value": p, "rule_id": rid, "rule_name": rid} for rid, p in entries]}}


def test_interface_planner_first_pattern_wins_and_keeps_hand_edits():
    current = inventory.current_values("device", {"_ifaces": [iface(1, "Gi1", if_alias="uplink"),
                                                                iface(2, "Gi2", monitored=False),
                                                                iface(3, "Lo0", if_type=24)]})
    plan = _plan(("a", {"set": "monitor", "field": "if_alias", "pattern": "uplink"}),
                 ("b", {"set": "ignore", "field": "if_name", "pattern": "."}))
    rules = {"a": {"revision": 1}, "b": {"revision": 1}}
    out = inventory.plan_changes("device", "d1", current, plan, {}, rules)
    assert [(c["item_key"], c["after"], c["rule_id"]) for c in out] == [("lo0", False, "b")]
    # Lo0 set off by rule b earlier, then turned on by hand: kept (set once), restored (enforce)
    owned = {("interfaces", "lo0"): {"value": False, "previous": True, "rule_id": "b"}}
    kept = inventory.plan_changes("device", "d1", current, plan, owned, rules)
    assert [(c["item_key"], c["outcome"]) for c in kept] == [("lo0", "kept_manual_edit")]
    rules["b"]["enforcement"] = "enforce"
    again = inventory.plan_changes("device", "d1", current, plan, owned, rules)
    assert [(c["item_key"], c["outcome"]) for c in again] == [("lo0", "apply")]


def test_interface_revert_on_unmatch_only_when_still_as_the_rule_set_it():
    current = inventory.current_values("device", {"_ifaces": [iface(1, "Lo0", monitored=False),
                                                                iface(2, "Lo1", monitored=True)]})
    owned = {("interfaces", "lo0"): {"value": False, "previous": True, "rule_id": "r"},
             ("interfaces", "lo1"): {"value": False, "previous": True, "rule_id": "r"}}
    out = inventory.plan_changes("device", "d1", current, {"settings": {}, "tags": []}, owned,
                                 {"r": {"revision": 2}}, revert_rules={"r"})
    assert [(c["item_key"], c["before"], c["after"], c["outcome"]) for c in out] == [("lo0", False, True, "revert")]


def test_agent_policy_skips_servers_without_an_agent():
    plan = {"settings": {"agent_policy": {"value": "11111111-1111-1111-1111-111111111111", "rule_id": "r"}},
            "tags": []}
    no_agent = inventory.current_values("server", {"agent_policy_id": None, "agent_count": 0})
    assert inventory.plan_changes("server", "s1", no_agent, plan, {}, {"r": {}}) == []
    with_agent = inventory.current_values("server", {"agent_policy_id": None, "agent_count": 1})
    assert [c["action_key"] for c in inventory.plan_changes("server", "s1", with_agent, plan, {}, {"r": {}})] == \
        ["agent_policy"]


def test_service_checks_are_never_reverted():
    current = inventory.current_values("device", {"_svc_templates": ["t1"]})
    owned = {("service_template", "t1"): {"value": "t1", "rule_id": "r"}}
    assert inventory.plan_changes("device", "d1", current, {"settings": {}, "tags": []}, owned, {"r": {}},
                                  revert_rules={"r"}) == []


def test_needs_names_the_loaders_rules_use():
    rules = [{"condition": {"op": "all", "conditions": [{"field": "neighbors"}, {"op": "any", "conditions": [
        {"field": "baseline_status"}]}]}, "actions": [{"key": "interfaces"}, {"key": "service_template"}]}]
    assert inventory.needs(rules) == {"topology", "baselines", "interfaces", "service_checks"}
    assert inventory.needs([{"condition": {"op": "all", "conditions": [{"field": "vendor"}]}, "actions": []}]) == set()


def _change(rule, asset, n=1):
    return [{"rule_id": rule, "asset_kind": "device", "asset_id": f"{asset}-{i}", "outcome": "apply"}
            for i in range(n)]


def test_limit_reasons_engine_cap_rule_cap_and_growth():
    settings = {"max_changes_per_run": 100, "growth_breaker_pct": 50, "growth_breaker_min": 20}
    r = {"id": "r", "name": "R", "max_changes_per_run": None, "last_match_count": None}
    assert reconcile.limit_reasons(_change("r", "a", 50), [r], settings) == []      # never evaluated: caps only
    assert "exceed the limit of 100" in reconcile.limit_reasons(_change("r", "a", 101), [r], settings)[0]
    r["max_changes_per_run"] = 10
    assert "its own limit is 10" in reconcile.limit_reasons(_change("r", "a", 11), [r], settings)[0]
    r.update(max_changes_per_run=None, last_match_count=100)
    assert reconcile.limit_reasons(_change("r", "a", 50), [r], settings) == []      # 50 = 50 % of 100
    assert "growth stop" in reconcile.limit_reasons(_change("r", "a", 51), [r], settings)[0]
    r["last_match_count"] = 0
    assert reconcile.limit_reasons(_change("r", "a", 20), [r], settings) == []      # the floor
    assert reconcile.limit_reasons(_change("r", "a", 21), [r], settings)
    assert reconcile.limit_reasons(_change("r", "a", 21), [r], {**settings, "growth_breaker_pct": 0}) == []


@pytest.mark.parametrize("now,every,at,expect", [
    ("2026-11-17T20:30:00+00:00", 24, "02:00", "2026-11-16T23:00:00+00:00"),
    ("2026-11-17T22:59:00+00:00", 24, "02:00", "2026-11-16T23:00:00+00:00"),
    ("2026-11-17T23:00:00+00:00", 24, "02:00", "2026-11-17T23:00:00+00:00"),
    ("2026-11-17T20:30:00+00:00", 6, "02:00", "2026-11-17T17:00:00+00:00"),
    ("2026-11-17T20:30:00+00:00", 168, "02:00", "2026-11-15T23:00:00+00:00"),   # Mondays
])
def test_schedule_slots(now, every, at, expect):
    settings = {"schedule_every_hours": every, "schedule_time": at, "schedule_timezone": "Asia/Riyadh"}
    assert reconcile.last_slot(datetime.fromisoformat(now), settings) == datetime.fromisoformat(expect)


def test_schedule_across_a_dst_change_keeps_local_time():
    settings = {"schedule_every_hours": 24, "schedule_time": "02:30", "schedule_timezone": "Europe/Berlin"}
    before = reconcile.last_slot(datetime(2026, 3, 28, 12, tzinfo=timezone.utc), settings)
    after = reconcile.last_slot(datetime(2026, 3, 30, 12, tzinfo=timezone.utc), settings)
    assert before == datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc)      # CET
    assert after == datetime(2026, 3, 30, 0, 30, tzinfo=timezone.utc)       # CEST
    nxt = reconcile.next_slot(datetime(2026, 3, 28, 12, tzinfo=timezone.utc), settings)
    assert nxt > datetime(2026, 3, 28, 12, tzinfo=timezone.utc)


def test_engine_merges_interface_patterns_without_duplicates():
    rules = [{"id": f"r{i}", "name": f"r{i}", "priority": i, "triggers": ["manual"], "asset_kinds": ["device"],
              "condition": {"op": "all", "conditions": []},
              "actions": [{"key": "interfaces", "value": [{"set": "ignore", "field": "if_type", "pattern": "24"}]}]}
             for i in (1, 2)]
    plan = engine.evaluate({"vendor": "x"}, rules, trigger="manual", kind="device")
    assert [e["rule_id"] for e in plan["additive"]["interfaces"]] == ["r1"]
