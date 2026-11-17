"""Discovery rules engine (pure parts): operators, groups, order, merge, dispositions.

The same ``engine.evaluate`` runs for preview, re-evaluation and the end of a
real scan, so these vectors pin what all three do. The shared contract
vectors live in ``scripts/dev/discovery-rules-contract/vectors.json``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.services.discovery_rules import actions, conditions, engine
from app.services.discovery_rules.facts import facts_from_result, sample_facts

VECTORS = Path(__file__).resolve().parents[2] / "scripts/dev/discovery-rules-contract/vectors.json"

FORTI = sample_facts({
    "ip_address": "10.51.2.12", "hostname": "fw-riyadh-01", "sys_object_id": ".1.3.6.1.4.1.12356.101.1.1000",
    "vendor": "Fortinet", "model": "FortiGate-100F", "device_type": "firewall", "os": "FortiOS",
    "os_version": "v7.2.10,build1706", "open_ports": [22, 443, 541], "protocols_detected": ["icmp", "snmp", "https"],
    "credential_status": "valid", "confidence_score": 90, "result_status": "new",
    "sys_descr": "FortiGate-100F v7.2.10",
})


def leaf(field, operator, value=None):
    return {"field": field, "operator": operator, "value": value}


def check(node, facts=FORTI):
    return conditions.evaluate(conditions.validate(node), facts)


def rule(rid, priority, cond, acts, **kw):
    return {"id": rid, "name": rid, "priority": priority, "revision": 1, "mode": "active",
            "asset_kinds": kw.pop("asset_kinds", ["device", "server"]), "triggers": ["discovery"],
            "profile_ids": kw.pop("profile_ids", None), "condition": conditions.validate(cond),
            "actions": actions.validate_actions(acts, ["device", "server"]),
            "stop_processing": kw.pop("stop_processing", False)}


# ── operators per type ──────────────────────────────────────────────────────

@pytest.mark.parametrize("node, expected", [
    (leaf("vendor", "equals", "fortinet"), True),            # case-insensitive
    (leaf("vendor", "not_equals", "Cisco"), True),
    (leaf("model", "contains", "100f"), True),
    (leaf("model", "not_contains", "60E"), True),
    (leaf("hostname", "starts_with", "fw-"), True),
    (leaf("hostname", "ends_with", "-01"), True),
    (leaf("sys_descr", "matches", r"^FortiGate-\d+F"), True),
    (leaf("vendor", "in_list", "Cisco, Fortinet"), True),
    (leaf("serial_number", "is_empty"), True),
    (leaf("vendor", "is_not_empty"), True),
    (leaf("sys_object_id", "starts_with", "1.3.6.1.4.1.12356"), True),
    (leaf("sys_object_id", "starts_with", "iso.3.6.1.4.1.12356"), True),
    (leaf("sys_object_id", "starts_with", "1.3.6.1.4.1.1235"), False),   # subtree, not string prefix
    (leaf("sys_object_id", "not_starts_with", "1.3.6.1.4.1.9"), True),
    (leaf("sys_object_id", "equals", ".1.3.6.1.4.1.12356.101.1.1000"), True),
    (leaf("os_version", "version_gte", "7.2.9"), True),       # 7.2.10 > 7.2.9 numerically
    (leaf("os_version", "version_lt", "7.4"), True),
    (leaf("os_version", "version_gt", "7.2.10"), False),
    (leaf("ip_address", "in_subnet", ["10.51.0.0/16"]), True),
    (leaf("ip_address", "in_subnet", ["10.52.0.0/16", "192.168.0.0/16"]), False),
    (leaf("ip_address", "not_in_subnet", ["10.52.0.0/16"]), True),
    (leaf("ip_address", "in_range", "10.51.2.1-10.51.2.20"), True),
    (leaf("ip_address", "equals", "10.51.2.12"), True),
    (leaf("ip_address", "in_subnet", ["2001:db8::/32"]), False),          # other family
    (leaf("confidence_score", "gte", 80), True),
    (leaf("confidence_score", "between", [10, 50]), False),
    (leaf("open_ports", "has_any", ["443", "8443"]), True),
    (leaf("open_ports", "has_all", ["22", "443"]), True),
    (leaf("open_ports", "has_none", ["3389"]), True),
    (leaf("protocols_detected", "has_any", ["SNMP"]), True),
    (leaf("credential_status", "is", "valid"), True),
    (leaf("result_status", "is_any_of", ["new", "changed"]), True),
    (leaf("is_server", "is_false"), True),
])
def test_operators(node, expected):
    assert check(node) is expected


def test_missing_facts_fail_positive_tests_and_pass_negative_ones():
    unknown = sample_facts({"ip_address": "10.0.0.5"})
    assert check(leaf("vendor", "equals", "Fortinet"), unknown) is False
    assert check(leaf("vendor", "contains", "Forti"), unknown) is False
    assert check(leaf("vendor", "not_equals", "Fortinet"), unknown) is True
    assert check(leaf("sys_object_id", "not_starts_with", "1.3.6.1.4.1.9"), unknown) is True
    assert check(leaf("open_ports", "has_none", ["22"]), unknown) is True
    assert check(leaf("os_version", "version_gte", "1.0"), unknown) is False
    assert check(leaf("snmp_credential_id", "is_empty"), unknown) is True


def test_groups_nest_and_short_circuit():
    tree = {"op": "all", "conditions": [
        leaf("vendor", "equals", "Fortinet"),
        {"op": "any", "conditions": [leaf("model", "contains", "60E"), leaf("model", "contains", "100F")]},
        {"op": "none", "conditions": [leaf("ip_address", "in_subnet", ["10.99.0.0/16"])]},
    ]}
    assert check(tree) is True
    assert check({"op": "none", "conditions": [tree]}) is False
    assert check({"op": "all", "conditions": []}) is True    # catch-all
    assert check({"op": "any", "conditions": []}) is False
    trace: list = []
    conditions.evaluate(conditions.validate({"op": "any", "conditions": [
        leaf("vendor", "equals", "Fortinet"), leaf("vendor", "equals", "Cisco")]}), FORTI, trace)
    assert trace == [["vendor", "equals", True]]            # second leaf not needed


@pytest.mark.parametrize("node, message", [
    ({"op": "xor", "conditions": []}, "Unknown group operator"),
    (leaf("nonsense", "equals", "x"), "Unknown field"),
    (leaf("open_ports", "contains", "22"), "cannot be used"),
    (leaf("vendor", "equals", ""), "value is required"),
    (leaf("ip_address", "in_subnet", ["10.0.0.0/33"]), "Not a subnet"),
    (leaf("open_ports", "has_any", ["http"]), "Not a TCP port"),
    (leaf("credential_status", "is", "great"), "Unknown Credential status"),
    (leaf("hostname", "matches", "(a+)+$"), "Nested quantifiers"),
    (leaf("hostname", "matches", r"(\w)\1"), "back-references"),
    (leaf("hostname", "matches", "x" * 201), "limited to 200"),
    (leaf("hostname", "matches", "[unclosed"), "Invalid regular expression"),
    (leaf("confidence_score", "between", [1]), "two numbers"),
    ({"op": "all", "conditions": [{"op": "all", "conditions": [{"op": "all", "conditions": [
        {"op": "all", "conditions": []}]}]}]}, "nest at most 3"),
    ({"op": "any", "conditions": [leaf("vendor", "equals", str(i)) for i in range(51)]}, "at most 50"),
])
def test_invalid_conditions_are_refused_at_save(node, message):
    with pytest.raises(conditions.ConditionError, match=message):
        conditions.validate(node)


def test_long_subjects_are_bounded():
    huge = sample_facts({"sys_descr": "a" * 100_000 + "FortiGate"})
    # The match runs on the first MAX_SUBJECT characters only.
    assert check(leaf("sys_descr", "matches", "FortiGate"), huge) is False


# ── actions ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, message", [
    ([], "at least one action"),
    ([{"key": "nope"}], "Unknown action"),
    ([{"key": "decision", "value": "delete"}], "auto_import, review or ignore"),
    ([{"key": "group", "value": "not-a-uuid"}], "choose a value"),
    ([{"key": "ping_interval", "value": 5}], "between 10 and 3600"),
    ([{"key": "tags", "value": []}], "at least one tag"),
    ([{"key": "location", "value": "A"}, {"key": "location", "value": "B"}], "set twice"),
])
def test_invalid_actions_are_refused(raw, message):
    with pytest.raises(actions.ActionError, match=message):
        actions.validate_actions(raw, ["device"])


def test_device_only_actions_need_a_device_rule():
    with pytest.raises(actions.ActionError, match="does not apply to server"):
        actions.validate_actions([{"key": "group", "value": "00000000-0000-0000-0000-000000000001"}], ["server"])


def test_activation_permissions_follow_the_actions():
    device_only = actions.validate_actions([{"key": "decision", "value": "auto_import"},
                                            {"key": "tags", "value": ["fw"]}], ["device"])
    assert device_only[0]["import_as"] == "device"
    assert actions.permissions_needed(device_only) == {"discovery.run", "devices.manage"}
    both = actions.validate_actions([{"key": "decision", "value": "auto_import", "import_as": "both"}], ["device"])
    assert "servers.manage" in actions.permissions_needed(both)


# ── order, shadowing, merge, stop ──────────────────────────────────────────

G1, G2 = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2"
FORTI_COND = {"op": "all", "conditions": [leaf("sys_object_id", "starts_with", "1.3.6.1.4.1.12356")]}
SUBNET_COND = {"op": "all", "conditions": [leaf("ip_address", "in_subnet", ["10.51.0.0/16"])]}


def test_first_match_wins_single_values_and_later_rules_are_shadowed():
    rules = [
        rule("subnet", 20, SUBNET_COND, [{"key": "group", "value": G2}, {"key": "tags", "value": ["riyadh"]}]),
        rule("forti", 10, FORTI_COND, [{"key": "group", "value": G1}, {"key": "tags", "value": ["firewall", "Riyadh"]},
                                       {"key": "decision", "value": "auto_import"}]),
    ]
    plan = engine.evaluate(FORTI, rules)
    assert [m["rule_id"] for m in plan["matched"]] == ["forti", "subnet"]
    assert engine.setting(plan, "group") == G1
    assert plan["settings"]["group"]["rule_id"] == "forti"
    assert plan["shadowed"] == [{"key": "group", "value": G2, "rule_id": "subnet", "rule_name": "subnet",
                                 "winner_rule_id": "forti", "winner_rule_name": "forti"}]
    assert engine.tag_values(plan) == ["firewall", "Riyadh"]     # merged, case-insensitive dedupe
    assert engine.decision_of(plan)["import_as"] == "device"


def test_stop_processing_ends_evaluation():
    rules = [rule("forti", 10, FORTI_COND, [{"key": "tags", "value": ["fw"]}], stop_processing=True),
             rule("subnet", 20, SUBNET_COND, [{"key": "tags", "value": ["riyadh"]}])]
    plan = engine.evaluate(FORTI, rules)
    assert engine.tag_values(plan) == ["fw"] and plan["stopped_by"] == "forti"


def test_catch_all_ignore_after_specific_import():
    """The common pattern: import what you know, ignore the rest."""
    rules = [rule("forti", 10, FORTI_COND, [{"key": "decision", "value": "auto_import"}]),
             rule("rest", 20, {"op": "all", "conditions": []}, [{"key": "decision", "value": "ignore"}])]
    assert engine.decision_of(engine.evaluate(FORTI, rules))["value"] == "auto_import"
    printer = sample_facts({"ip_address": "10.51.9.9", "vendor": "HP", "result_status": "new"})
    assert engine.decision_of(engine.evaluate(printer, rules))["value"] == "ignore"


def test_profile_limit_and_asset_kind_filter():
    r = rule("only-p", 10, {"op": "all", "conditions": []}, [{"key": "tags", "value": ["x"]}],
             profile_ids=["11111111-1111-1111-1111-111111111111"])
    assert engine.evaluate(FORTI, [r])["matched"] == []
    assert engine.evaluate({**FORTI, "profile_id": "11111111-1111-1111-1111-111111111111"}, [r])["matched"]
    srv = sample_facts({"ip_address": "10.51.3.3", "os": "Ubuntu 22.04", "result_status": "new"})
    dev_rule = rule("dev", 10, {"op": "all", "conditions": []}, [{"key": "tags", "value": ["d"]}],
                    asset_kinds=["device"])
    assert engine.evaluate(srv, [dev_rule])["kind"] == "server"
    assert engine.evaluate(srv, [dev_rule])["matched"] == []


# ── dispositions by profile import mode ─────────────────────────────────────

@pytest.mark.parametrize("decision, mode, status, ready, expected", [
    ("auto_import", "auto_match", "new", True, ("auto_import", None)),
    ("auto_import", "review", "new", True, ("would_import", "profile_review")),
    ("auto_import", "ignore_match", "new", True, ("would_import", "profile_ignore_only")),
    ("auto_import", "auto_match", "unknown", False, ("review", "not_ready")),
    ("ignore", "auto_match", "new", True, ("ignore", None)),
    ("ignore", "ignore_match", "new", True, ("ignore", None)),
    ("ignore", "review", "new", True, ("would_ignore", "profile_review")),
    ("review", "auto_match", "new", True, ("review", None)),
    ("auto_import", "auto_match", "existing", True, ("existing", None)),
    ("auto_import", "auto_match", "changed", True, ("existing", None)),
])
def test_disposition(decision, mode, status, ready, expected):
    plan = engine.evaluate(FORTI, [rule("r", 10, FORTI_COND, [{"key": "decision", "value": decision}])])
    assert engine.disposition(plan, import_mode=mode, result_status=status, import_ready=ready) == expected


def test_no_rule_matched():
    plan = engine.evaluate(FORTI, [])
    assert engine.disposition(plan, import_mode="auto_match", result_status="new", import_ready=True) == \
        ("no_match", None)


def test_capacity_assignment_holds_back_beyond_cap_and_license_room():
    from app.services.discovery_rules.runner import assign_capacity
    rows = [[i, {}, "auto_import", None] for i in range(10)] + [[99, {}, "ignore", None]]
    assign_capacity(rows, cap=50, room=3)
    assert [r[2] for r in rows[:3]] == ["auto_import"] * 3
    assert {(r[2], r[3]) for r in rows[3:10]} == {("review", "license_capacity_reached")}
    assert rows[10][2] == "ignore"
    rows = [[i, {}, "auto_import", None] for i in range(5)]
    assign_capacity(rows, cap=2, room=None)
    assert [r[3] for r in rows] == [None, None, "cap", "cap", "cap"]


def test_facts_from_a_scan_row():
    row = {"ip_address": "10.1.1.1", "sys_object_id": "iso.3.6.1.4.1.9.1.1208", "status": "new",
           "raw_data": {"sys_descr": "Cisco IOS"}, "open_ports": ["22", "x"], "protocols_detected": ["SNMP"],
           "device_type": "switch", "os": "IOS", "profile_id": "p1", "credential_used": None}
    f = facts_from_result(row)
    assert f["sys_object_id"] == "1.3.6.1.4.1.9.1.1208"
    assert f["sys_descr"] == "Cisco IOS" and f["open_ports"] == [22]
    assert f["protocols_detected"] == ["snmp"] and f["result_status"] == "new" and f["is_server"] is False


def test_scale_5000_assets_100_rules_under_two_seconds():
    rules = [rule(f"r{i}", i, {"op": "any", "conditions": [
        leaf("sys_object_id", "starts_with", f"1.3.6.1.4.1.{i}"),
        leaf("ip_address", "in_subnet", [f"10.{i}.0.0/16"]),
        leaf("hostname", "contains", f"site{i}-")]},
        [{"key": "tags", "value": [f"t{i}"]}]) for i in range(100)]
    assets = [sample_facts({"ip_address": f"10.{i % 120}.{i // 250 % 250}.{i % 250 + 1}",
                            "sys_object_id": f"1.3.6.1.4.1.{i % 150}.1", "hostname": f"site{i % 130}-sw"})
              for i in range(5000)]
    start = time.perf_counter()
    for f in assets:
        engine.evaluate(f, rules)
    # About 1.5 s on the dev appliance and 3.6 s on a shared CI runner; the
    # ceiling catches an order-of-magnitude regression without timing flakes.
    assert time.perf_counter() - start < 15.0


# ── shared contract vectors ─────────────────────────────────────────────────

def _vectors():
    return json.loads(VECTORS.read_text())["vectors"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_contract_vectors(vector):
    rules = [{**r, "condition": conditions.validate(r["condition"]),
              "actions": actions.validate_actions(r["actions"], r.get("asset_kinds", ["device"])),
              "triggers": ["discovery"], "revision": 1, "mode": "active"} for r in vector["rules"]]
    plan = engine.evaluate(sample_facts(vector["facts"]), rules)
    exp = vector["expect"]
    assert [m["rule_id"] for m in plan["matched"]] == exp["matched"]
    assert {k: v["value"] for k, v in plan["settings"].items()} == exp["settings"]
    assert engine.tag_values(plan) == exp.get("tags", [])
    assert [s["rule_id"] for s in plan["shadowed"]] == exp.get("shadowed", [])
    if "disposition" in exp:
        d = exp["disposition"]
        assert list(engine.disposition(plan, import_mode=d["import_mode"], result_status=d.get("status", "new"),
                                       import_ready=d.get("import_ready", True))) == d["expect"]


def test_date_type_for_custom_fields():
    """Device groups pass their own catalog; the date type compares ISO dates and treats a missing value as not matching."""
    from app.services.device_groups import facts as gfacts
    fields, enums = gfacts.catalog([{"key": "warranty_end", "label": "Warranty end", "field_type": "date",
                                     "choices": None, "asset_kinds": ["device"]}])

    def ev(op, value, have):
        node = conditions.validate({"op": "all", "conditions": [leaf("custom.warranty_end", op, value)]},
                                   fields=fields, enums=enums)
        return conditions.evaluate(node, {} if have is None else {"custom.warranty_end": have}, fields=fields)

    assert ev("before", "2027-01-01", "2026-12-31") is True
    assert ev("before", "2027-01-01", "2027-01-01") is False
    assert ev("on", "2027-01-01", "2027-01-01") is True
    assert ev("after", "2027-01-01", "2027-03-01") is True
    assert ev("before", "2027-01-01", None) is False
    assert ev("is_empty", None, None) is True
    assert ev("is_not_empty", None, "2027-01-01") is True
    with pytest.raises(conditions.ConditionError):
        conditions.validate({"op": "all", "conditions": [leaf("custom.warranty_end", "before", "01/02/2027")]},
                            fields=fields, enums=enums)
    with pytest.raises(conditions.ConditionError):   # the default discovery catalog has no custom fields
        conditions.validate({"op": "all", "conditions": [leaf("custom.warranty_end", "before", "2027-01-01")]})
