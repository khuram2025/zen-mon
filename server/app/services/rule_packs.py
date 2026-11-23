"""Recommended alert rules of a template ("rule packs", migrate-193).

A template recommends rules; nothing is created or enabled until an operator
accepts a recommendation, which creates one ordinary alert rule scoped to the
template (``alert_rules.profile_id``, covering templates customised from it)
and remembers the recommendation it came from (``pack_key``,
``pack_accepted``).

Recommendations come from two places:

* every gauge metric with display thresholds yields "above warn" (warning) and
  "above crit" (critical) rules on ``tpl_<metric>`` with a 5 minute hold, so a
  custom template recommends rules too;
* the template's own ``rule_pack`` (built-ins ship interface rules on roles:
  uplink down, WAN down, uplink utilisation), inherited by customised
  templates while they have none of their own;
* every enum (state) metric with an unambiguous bad state in its value map
  (``app.services.template_states``): HA out of sync, tunnel down, power
  supply or fan failed, BGP peer not established, pool member offline, red
  alarm. One incident per row, 2 minute hold, recovery notice on; critical
  P2 for power, HA, sync, BGP and red alarms, warning P3 otherwise.

Metrics of controller-managed child groups (FortiAPs, FortiSwitches) are
never recommended on the controller: they belong to the children.

A later template revision never changes an accepted rule. When the
recommendation differs from what was accepted, the template page shows
"recommendation changed" with Apply (update the rule) or Keep (keep the rule,
stop showing the notice).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import interface_roles as roles
from app.services import template_states as ts

logger = logging.getLogger(__name__)

VALUE_FIELDS = ("metric", "operator", "threshold", "reset_threshold", "min_duration", "clear_duration",
                "severity", "priority", "if_role")
DEFAULT_HOLD_S = 300
_OPS = (">", ">=", "<", "<=", "==", "!=")
_SEVERITIES = ("info", "warning", "critical")


class PackError(ValueError):
    pass


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _num(v) -> Optional[float]:
    return None if v is None else float(v)


def validate_pack(items: Any) -> Optional[list[dict]]:
    """Normalised explicit rule pack (None = none / as the parent). Raises PackError."""
    if items is None:
        return None
    if not isinstance(items, list) or len(items) > 100:
        raise PackError("rule_pack must be a list of at most 100 recommendations")
    out, seen = [], set()
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise PackError(f"recommendation {n} must be an object")
        key = str(it.get("key") or "").strip()
        if not key or len(key) > 80 or ":" in key or key in seen:
            raise PackError(f"recommendation {n}: key is required, unique, at most 80 characters, no ':'")
        seen.add(key)
        tree = None
        if it.get("condition_tree") is not None:
            # A combined device rule (multi-condition rules): the tree is the condition.
            from app.services import rule_conditions as rc
            try:
                tree = rc.validate_tree(it["condition_tree"], "device")
            except rc.TreeError as exc:
                raise PackError(f"recommendation {key}: {exc}") from None
            it = {**it, "metric": rc.COMBINED_METRIC, "operator": ">", "threshold": 0}
        metric = str(it.get("metric") or "")
        if not metric:
            raise PackError(f"recommendation {key}: metric is required")
        if it.get("operator") not in _OPS:
            raise PackError(f"recommendation {key}: operator must be one of {' '.join(_OPS)}")
        try:
            threshold = float(it.get("threshold"))
        except (TypeError, ValueError):
            raise PackError(f"recommendation {key}: threshold must be a number") from None
        sev = it.get("severity") or "warning"
        if sev not in _SEVERITIES:
            raise PackError(f"recommendation {key}: severity must be info, warning or critical")
        role = it.get("if_role") or None
        if role and role not in roles.ROLES:
            raise PackError(f"recommendation {key}: unknown interface role {role}")
        prio = it.get("priority") or None
        if prio and prio not in ("P1", "P2", "P3", "P4"):
            raise PackError(f"recommendation {key}: priority must be P1-P4")
        out.append({
            "key": key, "name": str(it.get("name") or key)[:200],
            "description": (str(it["description"])[:500] if it.get("description") else None),
            "metric": metric, "operator": it["operator"], "threshold": threshold,
            "reset_threshold": _num(it.get("reset_threshold")),
            "min_duration": int(it.get("min_duration") or 0),
            "clear_duration": int(it["clear_duration"]) if it.get("clear_duration") is not None else None,
            "severity": sev, "priority": prio, "if_role": role,
            **({"condition_tree": tree} if tree else {}),
        })
    return out


def _derived(groups: list[dict]) -> list[dict]:
    """"Above warn" / "above crit" recommendations from metric display thresholds.

    Groups of controller-managed children (FortiAPs, FortiSwitches: a ``children``
    mapping, ``display: "children"`` or ``belongs_to``) are skipped: their
    readings belong to the child devices, whose own cpu/memory series the
    ordinary device rules already cover (docs/controller-managed-children.md)."""
    out = []
    for g in groups or []:
        if ts.child_scoped(g):
            continue
        for m in g.get("metrics") or []:
            th = m.get("thresholds") or {}
            if (m.get("type") or "gauge") != "gauge" or not th:
                continue
            low = th.get("op") == "<="
            name = m.get("name") or m["key"]
            if g.get("kind") == "table" and g.get("name"):
                name = f"{g['name']}: {name}"
            unit = m.get("unit") or ""
            for level, sev in (("crit", "critical"), ("warn", "warning")):
                if th.get(level) is None:
                    continue
                value = float(th[level])
                shown = f"{value:g}{' ' + unit if unit and unit != '%' else unit}"
                out.append({
                    "key": f"{m['key']}:{level}",
                    "name": f"{name} {'below' if low else 'above'} {shown}",
                    "description": (f"Recommended from the template's {level} threshold for {name}; "
                                    "breaching for 5 minutes."),
                    "metric": f"tpl_{m['key']}", "operator": "<=" if low else ">=",
                    "threshold": value, "reset_threshold": None, "min_duration": DEFAULT_HOLD_S,
                    "clear_duration": None, "severity": sev, "priority": None, "if_role": None,
                    "source": "threshold",
                })
    return out


def recommendations(groups: list[dict], pack: Optional[list[dict]]) -> list[dict]:
    child = ts.child_metric_keys(groups)
    explicit = [{**it, "source": "template"} for it in (pack or [])
                if not (str(it.get("metric") or "").startswith("tpl_") and it["metric"][4:] in child)]
    derived = _derived(groups)
    taken = {it["key"] for it in explicit + derived}
    return explicit + derived + [it for it in ts.recommendations(groups) if it["key"] not in taken]


def values(item_or_rule: dict) -> dict:
    """The comparable recommendation values. ``states`` (the bad codes of a state
    recommendation) only appears when set, so threshold recommendations accepted
    before state rules existed compare unchanged."""
    out = {}
    for f in VALUE_FIELDS:
        v = item_or_rule.get(f)
        if f in ("threshold", "reset_threshold") and v is not None:
            v = float(v)
        if f in ("min_duration",) and v is not None:
            v = int(v)
        out[f] = v
    if item_or_rule.get("states") is not None:
        out["states"] = [int(c) for c in item_or_rule["states"]]
    if isinstance(item_or_rule.get("condition_tree"), dict):
        # A combined recommendation: a changed tree is a changed recommendation.
        out["condition_tree"] = item_or_rule["condition_tree"]
    return out


def changes(accepted: Optional[dict], current: dict) -> list[dict]:
    """What differs between the recommendation as accepted and as it is now."""
    if not accepted:
        return []
    a, c = values(accepted), values(current)
    return [{"field": f, "from": a.get(f), "to": c.get(f)} for f in VALUE_FIELDS + ("states",)
            if a.get(f) != c.get(f)]


ACCEPTED_SQL = """
    SELECT id::text, name, enabled, profile_id::text, pack_key, pack_accepted, threshold, conditions,
           min_duration, clear_duration, severity, priority, if_role, metric, operator
      FROM alert_rules WHERE pack_key IS NOT NULL AND profile_id = ANY(CAST(:ids AS uuid[]))
"""


async def state(db: AsyncSession, template: dict, family) -> list[dict]:
    """Every recommendation with the rule accepted for it (on this template or the one it
    customises) and whether the recommendation changed since."""
    pid = str(template["id"])
    lineage = family.ancestors(pid) or [pid]
    items = recommendations(_json(template.get("oid_groups")) or [], family.pack(pid))
    rows = (await db.execute(text(ACCEPTED_SQL), {"ids": lineage})).mappings().all()
    by_key: dict[tuple[str, str], dict] = {(r["profile_id"], r["pack_key"]): dict(r) for r in rows}
    names = {}
    if len(lineage) > 1:
        names = {str(r[0]): r[1] for r in (await db.execute(text(
            "SELECT id, name FROM device_profiles WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": lineage})).all()}
    out = []
    for it in items:
        rule = None
        owner = None
        for p in lineage:
            if (p, it["key"]) in by_key:
                rule, owner = by_key[(p, it["key"])], p
                break
        entry = {**it, "accepted": None, "status": "available", "changes": []}
        if rule:
            diff = changes(_json(rule.get("pack_accepted")), it)
            entry["accepted"] = {
                "rule_id": rule["id"], "name": rule["name"], "enabled": bool(rule["enabled"]),
                "on_this_template": owner == pid,
                "template_name": None if owner == pid else names.get(owner),
                "rule_values": values({**rule, "threshold": rule.get("threshold"),
                                       "reset_threshold": (_first_condition(rule) or {}).get("reset_threshold")}),
            }
            entry["status"] = "changed" if diff else "accepted"
            entry["changes"] = diff
        out.append(entry)
    return out


def _first_condition(rule: dict) -> Optional[dict]:
    conds = _json(rule.get("conditions"))
    return conds[0] if conds else None


def find(items: list[dict], key: str) -> Optional[dict]:
    return next((it for it in items if it["key"] == key), None)


def rule_fields(item: dict) -> dict:
    """The alert rule columns an accepted recommendation sets. A state
    recommendation brings its own AND-combined conditions on the one metric."""
    if item.get("condition_tree"):
        return {"metric": "combined", "operator": ">", "threshold": 0.0, "conditions": [],
                "condition_tree": item["condition_tree"], "subject_type": "device",
                "min_duration": int(item.get("min_duration") or 0), "clear_duration": item.get("clear_duration"),
                "severity": item.get("severity") or "warning", "priority": item.get("priority"), "if_role": None}
    if item.get("conditions"):
        conds = [{"metric": c["metric"], "operator": c["operator"], "threshold": float(c["threshold"])}
                 for c in item["conditions"]]
    else:
        cond = {"metric": item["metric"], "operator": item["operator"], "threshold": float(item["threshold"])}
        if item.get("reset_threshold") is not None:
            cond["reset_threshold"] = float(item["reset_threshold"])
        conds = [cond]
    return {
        "metric": item["metric"], "operator": item["operator"], "threshold": float(item["threshold"]),
        "conditions": conds, "min_duration": int(item.get("min_duration") or 0),
        "clear_duration": item.get("clear_duration"), "severity": item.get("severity") or "warning",
        "priority": item.get("priority"), "if_role": item.get("if_role"),
    }


def accepted_record(item: dict) -> str:
    return json.dumps(values(item))
