"""Condition trees (multi-condition rules, phase 1): validation, judging, holds, words."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import rule_conditions as rc

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
CPU = {"metric": "host_cpu_pct", "operator": ">", "threshold": 90}
MEM = {"metric": "host_memory_pct", "operator": ">", "threshold": 90}
NGINX = {"metric": "host_service_down", "target": "nginx"}


def tree(*items, logic="AND"):
    return rc.validate_tree({"logic": logic, "items": list(items)})


def R(**values):
    return rc.LeafReading(values=values)


# ── Validation ───────────────────────────────────────────────────────────────

def test_validate_assigns_ids_and_normalises_states():
    t = tree(CPU, MEM, {"logic": "or", "items": [NGINX, {"metric": "host_auto_service_stopped", "target": "Spool*"}]})
    assert [c["id"] for c in rc.leaves(t)] == ["c1", "c2", "c3", "c4"]
    assert t["items"][2]["logic"] == "OR"
    assert rc.leaves(t)[2] == {"id": "c3", "metric": "host_service_down", "operator": ">", "threshold": 0.0,
                               "target": "nginx"}
    assert rc.leaves(t)[3]["target"] == "Spool*"


@pytest.mark.parametrize("items, message", [
    ([CPU], "at least two"),
    ([CPU, {"metric": "host_restart"}], "event"),
    ([CPU, {"metric": "host_agent_offline"}], "heartbeat"),
    ([CPU, {"metric": "cpu", "operator": ">", "threshold": 1}], "cannot be combined"),
    ([CPU, {"metric": "host_service_down"}], "Name the service"),
    ([CPU, {"logic": "AND", "items": [MEM]}], "at least two"),
    ([CPU, {"logic": "AND", "items": [MEM, {"logic": "OR", "items": [CPU, MEM]}]}], "one level"),
    ([CPU, {**MEM, "operator": "~"}], "Use one of"),
    ([CPU, {**MEM, "threshold": "lots"}], "must be a number"),
    ([CPU, {**MEM, "reset_threshold": 95}], "healthy side"),
    ([CPU, {"metric": "host_fs_days_left", "operator": ">", "threshold": 5}], "use < or <="),
    ([CPU] * 11, "at most 10"),
])
def test_validate_refuses(items, message):
    with pytest.raises(rc.TreeError, match=message):
        rc.validate_tree({"logic": "AND", "items": items})


def test_validate_refuses_other_subjects_and_bad_logic():
    with pytest.raises(rc.TreeError, match="server rules"):
        rc.validate_tree({"logic": "AND", "items": [CPU, MEM]}, subject="device")
    with pytest.raises(rc.TreeError, match="Logic"):
        rc.validate_tree({"logic": "XOR", "items": [CPU, MEM]})


def test_duplicate_ids_are_renumbered():
    t = tree({**CPU, "id": "a"}, {**MEM, "id": "a"})
    assert [c["id"] for c in rc.leaves(t)] == ["a", "c2"]


def test_mirror_keeps_the_flat_columns_on_the_first_condition():
    m = rc.mirror(tree(CPU, MEM, logic="OR"))
    assert (m["metric"], m["operator"], m["threshold"], m["condition_logic"]) == ("host_cpu_pct", ">", 90.0, "OR")
    assert [c["metric"] for c in m["conditions"]] == ["host_cpu_pct", "host_memory_pct"]


# ── Judging ──────────────────────────────────────────────────────────────────

def test_and_needs_every_condition_and_unknown_never_fires():
    t = tree(CPU, MEM)
    assert rc.judge(t, {"c1": R(**{"": 95}), "c2": R(**{"": 93})}, active=False).state is True
    assert rc.judge(t, {"c1": R(**{"": 95}), "c2": R(**{"": 50})}, active=False).state is False
    assert rc.judge(t, {"c1": R(**{"": 95}), "c2": R(**{"": None})}, active=False).state is None
    assert rc.judge(t, {"c1": R(**{"": 95})}, active=False).state is None      # no reading at all


def test_or_fires_on_one_and_reports_only_what_matched():
    t = tree(CPU, MEM, NGINX, logic="OR")
    res = rc.judge(t, {"c1": R(**{"": 40}), "c2": R(**{"": None}), "c3": R(nginx=1.0)}, active=False)
    assert res.state is True and res.matched == ["c3"]


def test_the_owners_example_any_of_group_or_service():
    t = tree({"logic": "AND", "items": [CPU, MEM]}, {"metric": "host_auto_service_stopped"}, logic="OR")
    busy = {"c1": R(**{"": 95}), "c2": R(**{"": 92}), "c3": R(Spooler=0.0)}
    assert rc.judge(t, busy, active=False).matched == ["c1", "c2"]
    stopped = {"c1": R(**{"": 20}), "c2": R(**{"": 92}), "c3": R(Spooler=1.0, W32Time=0.0)}
    res = rc.judge(t, stopped, active=False)
    assert res.state is True and res.matched == ["c3"]
    assert res.leaves["c3"].matched == [("Spooler", 1.0)]


def test_any_instance_and_unreadable_sources():
    fs = {"metric": "host_filesystem_pct", "operator": ">", "threshold": 90}
    t = tree(CPU, fs)
    res = rc.judge(t, {"c1": R(**{"": 95}), "c2": R(**{"/": 50, "/data": 96})}, active=False)
    assert res.state is True and res.leaves["c2"].matched == [("/data", 96)]
    # One mount unknown, none over: unknown, never false.
    assert rc.judge(t, {"c1": R(**{"": 95}), "c2": R(**{"/": 50, "/data": None})}, active=False).state is None
    # A source that could not be read is unknown.
    unread = rc.LeafReading(known=False)
    assert rc.judge(t, {"c1": R(**{"": 95}), "c2": unread}, active=False).state is None
    # Nothing to watch (no automatic services) is not a breach.
    t2 = tree(CPU, {"metric": "host_auto_service_stopped"})
    assert rc.judge(t2, {"c1": R(**{"": 95}), "c2": R()}, active=False).state is False


def test_open_incident_judges_clear_levels():
    t = tree(CPU, {**MEM, "reset_threshold": 80})
    # 89% CPU is under the 90% trigger but above the automatic 88% clear level.
    held = rc.judge(t, {"c1": R(**{"": 89}), "c2": R(**{"": 85})}, active=True)
    assert held.state is True
    cleared = rc.judge(t, {"c1": R(**{"": 89}), "c2": R(**{"": 79})}, active=True)
    assert cleared.state is False
    days = tree(CPU, {"metric": "host_fs_days_left", "operator": "<", "threshold": 7})
    # Fewer than 7 days fires; it clears only at 8 or more (one day on the healthy side).
    assert rc.judge(days, {"c1": R(**{"": 95}), "c2": R(**{"/": 7.5})}, active=True).state is True


# ── Holds ────────────────────────────────────────────────────────────────────

def test_step_breach_hold_and_clear_hold():
    s = rc.step(True, open_incident=False, breach_since=None, clear_since=None, hold_s=300,
                clear_hold_s=300, now=NOW)
    assert (s.action, s.breach_since) == ("hold", NOW)
    s = rc.step(True, open_incident=False, breach_since=NOW - timedelta(seconds=299), clear_since=None,
                hold_s=300, clear_hold_s=300, now=NOW)
    assert s.action == "hold"
    s = rc.step(True, open_incident=False, breach_since=NOW - timedelta(seconds=300), clear_since=None,
                hold_s=300, clear_hold_s=300, now=NOW)
    assert s.action == "raise"
    assert rc.step(True, open_incident=False, breach_since=None, clear_since=None, hold_s=0,
                   clear_hold_s=0, now=NOW).action == "raise"
    s = rc.step(False, open_incident=True, breach_since=None, clear_since=None, hold_s=0,
                clear_hold_s=300, now=NOW)
    assert (s.action, s.clear_since) == ("hold", NOW)
    assert rc.step(False, open_incident=True, breach_since=None, clear_since=NOW - timedelta(seconds=300),
                   hold_s=0, clear_hold_s=300, now=NOW).action == "resolve"
    assert rc.step(True, open_incident=True, breach_since=None, clear_since=NOW, hold_s=0,
                   clear_hold_s=300, now=NOW).action == "continue"


def test_step_unknown_restarts_clocks_and_changes_nothing():
    s = rc.step(None, open_incident=True, breach_since=None, clear_since=NOW - timedelta(seconds=200),
                hold_s=0, clear_hold_s=300, now=NOW)
    assert (s.action, s.breach_since, s.clear_since) == ("none", None, None)
    s = rc.step(None, open_incident=False, breach_since=NOW - timedelta(seconds=200), clear_since=None,
                hold_s=300, clear_hold_s=300, now=NOW)
    assert (s.action, s.breach_since) == ("none", None)


# ── Overrides and words ──────────────────────────────────────────────────────

def test_override_changes_the_named_condition_only():
    t = tree(CPU, MEM, NGINX)
    o = SimpleNamespace(metric="host_memory_pct", threshold=95.0, reset_threshold=None)
    out = rc.apply_override(t, o)
    assert [c["threshold"] for c in rc.leaves(out)] == [90.0, 95.0, 0.0]
    assert [c["threshold"] for c in rc.leaves(t)] == [90.0, 90.0, 0.0]          # original untouched
    first = rc.apply_override(t, SimpleNamespace(metric=None, threshold=80.0, reset_threshold=None))
    assert rc.leaves(first)[0]["threshold"] == 80.0
    # A state condition has no threshold to override.
    same = rc.apply_override(t, SimpleNamespace(metric="host_service_down", threshold=3.0, reset_threshold=None))
    assert same is t


def test_labels_and_descriptions():
    t = tree({"logic": "AND", "items": [CPU, MEM]}, NGINX, logic="OR")
    label = rc.tree_label(t)
    assert label.startswith("(") and " or service nginx stopped" in label.lower()
    assert "and" in label
    res = rc.judge(t, {"c1": R(**{"": 94}), "c2": R(**{"": 92}), "c3": R(nginx=0.0)}, active=False)
    words = rc.describe(t, res, {})
    assert words.count("(limit") == 2 and " and " in words
    meta = rc.matched_metadata(t, res)
    assert [m["id"] for m in meta] == ["c1", "c2"] and meta[0]["value"] == 94
