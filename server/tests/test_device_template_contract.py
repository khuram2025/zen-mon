"""Device Templates: classification contract and overlay resolution (pure, no database).

The classification vectors are shared with the poller
(poller/internal/checker/snmp/profile_contract_test.go): both implementations
must pick the same template for every case, or the device page, discovery
import and the poller disagree about what a device is.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.snmp import MatchRules
from app.services.device_templates import classify
from app.services.device_templates import overlay as ov

VECTORS = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "template-match-contract" / "vectors.json"
SPEC = json.loads(VECTORS.read_text())


@pytest.mark.parametrize("case", SPEC["cases"], ids=[c["name"] for c in SPEC["cases"]])
@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_contract_vectors(case, order):
    templates = list(SPEC["templates"]) if order == "forward" else list(reversed(SPEC["templates"]))
    best = classify.match(templates, case["sys_object_id"], case["sys_descr"])
    assert (best["id"] if best else None) == case["expect"]


def test_explain_names_the_winner_and_every_reason():
    out = classify.explain(SPEC["templates"], "1.3.6.1.4.1.2636.1.1.1.2.39", "Juniper Networks, Inc. srx340")
    assert out["winner"]["name"] == "Juniper SRX"
    by_name = {c["name"]: c for c in out["candidates"]}
    assert by_name["Juniper SRX"]["rank"] == 1 and by_name["Juniper JunOS"]["rank"] == 2
    assert any("prefix" in r for r in by_name["Juniper JunOS"]["reasons"])
    assert by_name["Manual only"]["matched"] is False
    assert by_name["Broken regex"]["reasons"][0].startswith("sysDescr pattern is not usable")


@pytest.mark.parametrize("pattern", ["(?=a)b", "(?<!x)y", r"(a)\1", "(?P<n>a)(?P=n)", "(?>ab)", "a++", "(?(1)a|b)"])
def test_patterns_the_poller_cannot_run_are_refused(pattern):
    assert classify.regex_problem(pattern)
    with pytest.raises(ValueError):
        MatchRules(sys_descr_regex=pattern)


@pytest.mark.parametrize("pattern", ["(?i)FortiGate|FortiOS", r"Version ([^,\s]+)", r"(?P<v>\d+)\.x", r"\++"])
def test_portable_patterns_are_accepted(pattern):
    assert classify.regex_problem(pattern) is None


def test_match_rules_normalise_prefixes():
    rules = MatchRules(sys_object_id_prefixes=[".1.3.6.1.4.1.9", " 1.3.6.1.4.1.9", "", "1.3.6.1.4.1.12356"])
    assert rules.sys_object_id_prefixes == ["1.3.6.1.4.1.9", "1.3.6.1.4.1.12356"]
    with pytest.raises(ValueError):
        MatchRules(sys_object_id_prefixes=["enterprises.9"])


def test_extract_reads_named_or_first_group_and_falls_back_to_defaults():
    t = {"vendor": "Fortinet", "match_rules": {"extract_model": r"(Forti[A-Za-z]+-[0-9A-Za-z]+)",
                                               "extract_os_version": r"v(?P<v>[0-9.]+)",
                                               "default_vendor": "Fortinet"}}
    assert classify.extract(t, "FortiGate-60F v7.2.5,build1517") == ("Fortinet", "FortiGate-60F", "7.2.5")
    assert classify.extract(t, "") == ("Fortinet", "", "")


# ── Overlays ────────────────────────────────────────────────────────────────

PARENT = [
    {"key": "system", "name": "System", "kind": "scalar", "metrics": [
        {"key": "cpu", "name": "CPU", "oid": "1.3.6.1.4.1.1.1", "unit": "%",
         "thresholds": {"warn": 80, "crit": 90, "op": ">="}},
        {"key": "mem", "name": "Memory", "oid": "1.3.6.1.4.1.1.2"},
        {"key": "disk", "name": "Disk", "oid": "1.3.6.1.4.1.1.3"},
    ]},
    {"key": "vpn", "name": "VPN", "kind": "table", "interval_seconds": 60, "table": {"label_oid": "1.3.6.1.4.1.1.9.1"},
     "metrics": [{"key": "tun_state", "name": "State", "oid": "1.3.6.1.4.1.1.9.2", "type": "enum"}]},
    {"key": "ha", "name": "HA", "metrics": [{"key": "ha_mode", "name": "Mode", "oid": "1.3.6.1.4.1.1.5"}]},
]


def test_overlay_resolution_applies_every_kind_of_change():
    doc = {
        "disable_groups": ["ha"],
        "disable_metrics": {"system": ["disk"]},
        "group_changes": {"vpn": {"interval_seconds": 300}},
        "metric_changes": {"system": {"cpu": {"thresholds": {"warn": 85, "crit": 95, "op": ">="}}}},
        "add_metrics": {"system": [{"key": "sessions", "name": "Sessions", "oid": "1.3.6.1.4.1.1.4"}]},
        "add_groups": [{"key": "lab", "name": "Lab", "metrics": [{"key": "lab_x", "name": "X", "oid": "1.3.6.1.4.1.2.1"}]}],
    }
    groups, warnings = ov.resolve(PARENT, doc)
    assert warnings == []
    assert [g["key"] for g in groups] == ["system", "vpn", "lab"]
    system = groups[0]
    assert [m["key"] for m in system["metrics"]] == ["cpu", "mem", "sessions"]
    assert system["metrics"][0]["thresholds"]["crit"] == 95
    assert groups[1]["interval_seconds"] == 300
    assert PARENT[0]["metrics"][0]["thresholds"]["crit"] == 90          # the parent is never mutated


def test_overlay_survives_a_parent_update_and_reports_what_it_skipped():
    doc = {"disable_metrics": {"system": ["disk"]},
           "metric_changes": {"system": {"cpu": {"unit": "pct"}}, "gone": {"x": {"unit": "%"}}},
           "add_groups": [{"key": "vpn", "name": "Mine", "metrics": []}]}
    updated = [dict(PARENT[0], metrics=[m for m in PARENT[0]["metrics"] if m["key"] != "disk"]
                    + [{"key": "temp", "name": "Temp", "oid": "1.3.6.1.4.1.1.7"}]), PARENT[1]]
    groups, warnings = ov.resolve(updated, doc)
    keys = [m["key"] for m in groups[0]["metrics"]]
    assert keys == ["cpu", "mem", "temp"]                                # new parent metric arrives
    assert groups[0]["metrics"][0]["unit"] == "pct"                      # customer change kept
    assert any("disk" in w for w in warnings)
    assert any("gone" in w for w in warnings)
    assert any("'vpn'" in w and "skipped" in w for w in warnings)


def test_editing_effective_groups_round_trips_through_an_overlay():
    edited, _ = ov.resolve(PARENT, {})
    edited[0]["metrics"][0]["thresholds"] = {"warn": 70, "crit": 85, "op": ">="}
    edited[0]["metrics"].pop(2)
    edited[1]["interval_seconds"] = 600
    edited = [g for g in edited if g["key"] != "ha"]
    edited.append({"key": "extra", "name": "Extra", "metrics": [{"key": "e1", "name": "E1", "oid": "1.3.6.1.4.1.3.1"}]})
    doc = ov.diff_to_overlay(PARENT, edited)
    assert doc == {
        "disable_groups": ["ha"],
        "add_groups": [edited[-1]],
        "group_changes": {"vpn": {"interval_seconds": 600}},
        "metric_changes": {"system": {"cpu": {"thresholds": {"warn": 70, "crit": 85, "op": ">="}}}},
        "disable_metrics": {"system": ["disk"]},
    }
    resolved, warnings = ov.resolve(PARENT, doc)
    assert resolved == edited and warnings == []


def test_unchanged_groups_with_implicit_defaults_give_an_empty_overlay():
    explicit = [dict(g, kind=g.get("kind") or "scalar", interval_seconds=g.get("interval_seconds") or 0,
                     metrics=[dict(m, type=m.get("type") or "gauge") for m in g["metrics"]]) for g in PARENT]
    assert ov.diff_to_overlay(PARENT, explicit) == {}
    assert ov.problems_from_edit(PARENT, explicit) == []


def test_inherited_oids_and_tables_cannot_be_changed_in_an_overlay():
    edited, _ = ov.resolve(PARENT, {})
    edited[0]["metrics"][0]["oid"] = "1.3.6.1.4.1.9.9"
    edited[1]["table"] = {"label_oid": "1.3.6.1.4.1.9.1"}
    errs = ov.problems_from_edit(PARENT, edited)
    assert any("'oid'" in e for e in errs) and any("'table'" in e for e in errs)


def test_overlay_problems_catch_key_collisions():
    errs = ov.problems({"add_groups": [{"key": "system", "metrics": []}],
                        "add_metrics": {"system": [{"key": "cpu"}], "nope": [{"key": "x"}]}}, PARENT)
    assert len(errs) == 3
