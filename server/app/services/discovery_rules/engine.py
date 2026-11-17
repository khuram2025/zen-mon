"""The pure part of the engine: ``evaluate(facts, rules) -> plan``.

Preview, the re-evaluation of a past scan and the evaluation at the end of a
real scan all call this one function, so what a preview shows is what a run
applies.

Order and conflicts
-------------------
Rules are evaluated in ascending ``priority`` (lower number first). For each
single-valued action (import decision, group, template, ...) the first
matching rule sets the value; a later matching rule that sets the same action
is recorded in ``shadowed``. Tags from every matching rule are merged. A
matching rule with ``stop_processing`` ends the evaluation. Priorities are
unique, so there are no ties.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from . import conditions
from .actions import ACTIONS
from .facts import asset_kind_of

TRACE_RULE_LIMIT = 25


def _rule_applies(rule: dict, facts: dict, trigger: str, kind: str) -> str | None:
    """None when the rule should be evaluated, else the reason it is skipped."""
    if trigger not in (rule.get("triggers") or ["discovery"]):
        return "trigger"
    if kind not in (rule.get("asset_kinds") or ["device"]):
        return "asset_kind"
    profiles = rule.get("profile_ids")
    if profiles:
        if str(facts.get("profile_id") or "") not in {str(p) for p in profiles}:
            return "profile"
    return None


def evaluate(facts: dict, rules: Iterable[dict], *, trigger: str = "discovery",
             kind: str | None = None) -> dict:
    """The plan the rules make for one asset.

    ``rules`` are plain dicts (id, name, priority, revision, asset_kinds,
    triggers, profile_ids, condition, actions, stop_processing); the caller
    decides which modes take part."""
    kind = kind or asset_kind_of(facts)
    settings: dict[str, dict] = {}
    tags: list[dict] = []
    additive: dict[str, list[dict]] = {}
    shadowed: list[dict] = []
    matched: list[dict] = []
    trace: list[dict] = []
    stopped_by = None

    for rule in sorted(rules, key=lambda r: r["priority"]):
        rid = str(rule["id"])
        ref = {"rule_id": rid, "rule_name": rule.get("name")}
        skip = _rule_applies(rule, facts, trigger, kind)
        if skip:
            if len(trace) < TRACE_RULE_LIMIT:
                trace.append({"rule_id": rid, "rule_name": rule.get("name"), "matched": False, "skipped": skip})
            continue
        leaves: list = []
        ok = conditions.evaluate(rule["condition"], facts, leaves, strict_booleans=True)
        if len(trace) < TRACE_RULE_LIMIT:
            trace.append({"rule_id": rid, "rule_name": rule.get("name"), "matched": ok, "leaves": leaves})
        if not ok:
            continue
        matched.append({"rule_id": rid, "rule_name": rule.get("name"), "priority": rule["priority"],
                        "revision": rule.get("revision")})
        for action in rule.get("actions") or []:
            key = action["key"]
            spec = ACTIONS.get(key)
            if spec is None or kind not in spec.asset_kinds:
                continue
            if spec.kind == "additive" and key == "tags":
                have = {t["value"].lower() for t in tags}
                for value in action["value"]:
                    if value.lower() not in have:
                        tags.append({"value": value, **ref})
                        have.add(value.lower())
                continue
            if spec.kind == "additive":
                # other additive actions (network maps): one entry per value,
                # merged across matching rules
                lst = additive.setdefault(key, [])
                values = action["value"] if isinstance(action["value"], list) else [action["value"]]
                for value in values:
                    if str(value).lower() not in {str(e["value"]).lower() for e in lst}:
                        lst.append({"value": value, **ref})
                continue
            entry = {"value": action["value"], **ref}
            if "import_as" in action:
                entry["import_as"] = action["import_as"]
            if key in settings:
                winner = settings[key]
                shadowed.append({"key": key, "value": action["value"], **ref,
                                 "winner_rule_id": winner["rule_id"], "winner_rule_name": winner["rule_name"]})
            else:
                settings[key] = entry
        if rule.get("stop_processing"):
            stopped_by = rid
            break

    return {
        "kind": kind,
        "matched": matched,
        "settings": settings,
        "tags": tags,
        "additive": additive,
        "shadowed": shadowed,
        "stopped_by": stopped_by,
        "trace": trace,
    }


def decision_of(plan: dict | None) -> dict | None:
    return ((plan or {}).get("settings") or {}).get("decision")


def setting(plan: dict | None, key: str) -> Any:
    entry = ((plan or {}).get("settings") or {}).get(key)
    return entry.get("value") if entry else None


def tag_values(plan: dict | None) -> list[str]:
    return [t["value"] for t in (plan or {}).get("tags") or []]


def disposition(plan: dict, *, import_mode: str | None, result_status: str | None,
                import_ready: bool) -> tuple[str, str | None]:
    """What should happen to one scan result, before capacity is considered.

    ``import_mode`` is the profile's: ``review`` lets rules annotate only,
    ``auto_match`` honours auto-import and ignore, ``ignore_match`` honours
    ignore only. Returns (disposition, reason):

    * ``auto_import`` / ``ignore``  - act on it now;
    * ``would_import`` / ``would_ignore`` - the profile does not allow it;
    * ``review`` - a rule kept it for review, or it is not ready to import;
    * ``existing`` - already monitored (phase 1 never changes inventory);
    * ``no_match`` - no rule matched.
    """
    if result_status in ("existing", "changed"):
        return "existing", None
    decision = decision_of(plan)
    if not decision:
        return ("review" if plan.get("matched") else "no_match"), None
    value = decision["value"]
    mode = import_mode or "review"
    if value == "ignore":
        return ("ignore", None) if mode in ("auto_match", "ignore_match") else ("would_ignore", "profile_review")
    if value == "auto_import":
        if mode != "auto_match":
            return "would_import", "profile_review" if mode == "review" else "profile_ignore_only"
        if not import_ready:
            return "review", "not_ready"
        return "auto_import", None
    return "review", None


def compact(plan: dict) -> dict:
    """The plan as stored on a scan result: no evaluation trace for rules
    that were skipped, leaf traces kept for the "why" popover."""
    out = dict(plan)
    out["trace"] = [t for t in plan.get("trace") or [] if not t.get("skipped")]
    return out


def rules_revision(rules: Iterable[dict]) -> str:
    h = hashlib.sha256()
    for r in sorted(rules, key=lambda r: r["priority"]):
        h.update(f"{r['id']}:{r.get('revision')}:{r['priority']}:{r.get('mode')};".encode())
    return h.hexdigest()[:16]
