"""Condition trees: several conditions in one alert rule (docs/multi-condition-rules.md).

A tree is ``{"logic": "AND"|"OR", "items": [...]}`` where an item is a
condition or, directly under the root only, a group of conditions with its
own logic. Each condition is ``{"id", "metric", "operator", "threshold",
"reset_threshold"?, "target"?}``; ``target`` names one instance (a service, a
process, a mount, a disk, a NIC) or, for the automatic-services condition,
service name patterns to leave out. A condition without a target on a metric
that has instances is true when any instance is.

This module is pure: it validates and labels trees and judges one subject's
readings. Each evaluator supplies the readings (``LeafReading`` per condition)
and keeps the hold clocks between passes (``step``). Phase 1 serves server
rules (``host_alert_service``).

Judging follows the device engine (``network_conditions``): three-valued
logic, so an unknown reading never raises or resolves an incident; once an
incident is open each condition is judged against its clear level.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.services import network_conditions as nc

MAX_CONDITIONS = 10
LOGICS = ("AND", "OR")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class TreeError(ValueError):
    """A tree that cannot be stored or evaluated; the message is user-facing."""


# ── Catalogue (what a server rule may combine) ───────────────────────────────

@dataclass(frozen=True)
class MetricSpec:
    kind: str                  # "level" (numeric) or "state" (1 = the bad state)
    instance: Optional[str]    # instance noun when the metric has instances
    target: str = "none"       # "none" | "optional" (instance) | "required" | "exclude"


SERVER_METRICS: dict[str, MetricSpec] = {
    "host_cpu_pct": MetricSpec("level", None),
    "host_memory_pct": MetricSpec("level", None),
    "host_disk_util_pct": MetricSpec("level", None),
    "host_swap_pct": MetricSpec("level", None),
    "host_clock_skew_s": MetricSpec("level", None),
    "host_filesystem_pct": MetricSpec("level", "filesystem", "optional"),
    "host_disk_latency_ms": MetricSpec("level", "disk", "optional"),
    "host_nic_errors": MetricSpec("level", "network interface", "optional"),
    "host_fs_days_left": MetricSpec("level", "filesystem", "optional"),
    "host_service_down": MetricSpec("state", "service", "required"),
    "host_process_down": MetricSpec("state", "process", "required"),
    "host_auto_service_stopped": MetricSpec("state", "automatic service", "exclude"),
}
# Events and availability stand alone: a restart is not "true for a while",
# and Down / agent offline have their own signal and paging rules.
_ALONE = {
    "host_restart": "A restart or shutdown is an event; it needs a rule of its own",
    "host_shutdown": "A restart or shutdown is an event; it needs a rule of its own",
    "host_agent_offline": "Agent offline needs a rule of its own (it is judged on the agent heartbeat)",
}
_LOW = ("<", "<=", "lt", "lte")
_HIGH = (">", ">=", "gt", "gte")


# ── Shape ────────────────────────────────────────────────────────────────────

def is_group(item) -> bool:
    return isinstance(item, dict) and "items" in item


def leaves(tree: Optional[dict]) -> list[dict]:
    """Every condition in reading order."""
    out: list[dict] = []
    for item in (tree or {}).get("items") or []:
        if is_group(item):
            out.extend(c for c in item.get("items") or [] if isinstance(c, dict))
        elif isinstance(item, dict):
            out.append(item)
    return out


def parse(value) -> Optional[dict]:
    """A stored tree (JSON text or dict) or None."""
    if value is None:
        return None
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and value.get("items") else None


def _number(value, what: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise TreeError(f"{what} must be a number") from None
    if not math.isfinite(n):
        raise TreeError(f"{what} must be a finite number")
    return n


def _normalize_leaf(c: dict, used: set[str], n: int) -> dict:
    if not isinstance(c, dict):
        raise TreeError("Each condition must be an object")
    metric = c.get("metric")
    if metric in _ALONE:
        raise TreeError(_ALONE[metric])
    spec = SERVER_METRICS.get(metric)
    if spec is None:
        raise TreeError(f"{metric or 'This metric'} cannot be combined in a server rule")
    cid = str(c.get("id") or "")
    if not cid or cid in used:
        cid = f"c{n}"
        while cid in used:
            n += 1
            cid = f"c{n}"
    if not _ID_RE.match(cid):
        raise TreeError("A condition id is 1-16 letters, digits, - or _")
    used.add(cid)
    target = (str(c.get("target")).strip() if c.get("target") is not None else "") or None
    if spec.target == "required" and not target:
        raise TreeError(f"Name the {spec.instance} this condition watches")
    if spec.target == "none":
        target = None
    out = {"id": cid, "metric": metric}
    if spec.kind == "state":
        # A state reads 1 while it is bad: the condition is always "> 0".
        out.update(operator=">", threshold=0.0)
    else:
        op = c.get("operator")
        if op not in nc.OPS:
            raise TreeError("Use one of >, >=, <, <=, == or !=")
        thr = _number(c.get("threshold"), "The limit")
        if metric == "host_fs_days_left":
            if op not in _LOW:
                raise TreeError("A days-until-full condition fires below a number of days: use < or <=")
            if thr <= 0:
                raise TreeError("Days until full must be more than 0")
        out.update(operator=op, threshold=thr)
        reset = c.get("reset_threshold")
        if reset is not None:
            reset = _number(reset, "The clear level")
            if not ((op in _HIGH and reset <= thr) or (op in _LOW and reset >= thr)):
                raise TreeError("A clear level must lie on the healthy side of its limit")
            out["reset_threshold"] = reset
    if target:
        out["target"] = target[:255]
    return out


def validate_tree(tree, subject: str = "server") -> dict:
    """The tree as stored (ids assigned, values normalised). Raises TreeError."""
    if subject != "server":
        raise TreeError("Combined conditions are available for server rules in this version")
    if not isinstance(tree, dict) or not isinstance(tree.get("items"), list):
        raise TreeError("Conditions must be a group with a logic and a list of items")
    logic = str(tree.get("logic") or "AND").upper()
    if logic not in LOGICS:
        raise TreeError("Logic must be AND (all of) or OR (any of)")
    used: set[str] = set()
    items: list = []
    n = 0
    for item in tree["items"]:
        if is_group(item):
            sub_logic = str(item.get("logic") or "AND").upper()
            if sub_logic not in LOGICS:
                raise TreeError("Logic must be AND (all of) or OR (any of)")
            sub = item.get("items")
            if not isinstance(sub, list) or len(sub) < 2:
                raise TreeError("A group holds at least two conditions")
            if any(is_group(s) for s in sub):
                raise TreeError("Groups can be one level deep: a group cannot hold another group")
            norm = []
            for s in sub:
                n += 1
                norm.append(_normalize_leaf(s, used, n))
            items.append({"logic": sub_logic, "items": norm})
        else:
            n += 1
            items.append(_normalize_leaf(item, used, n))
    count = len(used)
    if count < 2:
        raise TreeError("A combined rule needs at least two conditions")
    if count > MAX_CONDITIONS:
        raise TreeError(f"A rule can combine at most {MAX_CONDITIONS} conditions")
    return {"logic": logic, "items": items}


def mirror(tree: dict) -> dict:
    """The flat columns kept in step with a tree (the first condition), so
    lists, exports and older pages keep reading a sensible rule."""
    first = leaves(tree)[0]
    return {"metric": first["metric"], "operator": first["operator"],
            "threshold": float(first["threshold"]),
            "conditions": [{k: v for k, v in c.items() if k != "id"} for c in leaves(tree)],
            "condition_logic": tree["logic"] if not any(is_group(i) for i in tree["items"]) else "AND"}


# ── Words ────────────────────────────────────────────────────────────────────

def leaf_label(c: dict) -> str:
    """"CPU above 90%", "Service nginx stopped", "Filesystem /data above 85%"."""
    from app.services import alert_phrasing as ap
    metric, target = c.get("metric"), c.get("target")
    if metric == "host_service_down":
        return f"Service {target} stopped"
    if metric == "host_process_down":
        return f"Process {target} not running"
    if metric == "host_auto_service_stopped":
        return "An automatic service stopped" + (f" (except {target})" if target else "")
    label = ap.condition_label(metric, c.get("operator"), c.get("threshold"))
    spec = SERVER_METRICS.get(metric)
    if spec and spec.instance and target:
        noun = ap.metric_noun(metric)
        if label.startswith(noun):
            return f"{noun} {target}{label[len(noun):]}"
        return f"{label} ({target})"
    return label


def tree_label(tree: Optional[dict]) -> str:
    """"CPU above 90% and memory above 90%", "(A and B) or C"."""
    if not tree:
        return ""
    root = " or " if tree.get("logic") == "OR" else " and "
    parts = []
    for item in tree.get("items") or []:
        if is_group(item):
            joiner = " or " if item.get("logic") == "OR" else " and "
            inner = joiner.join(_lower_first(leaf_label(c), i) for i, c in enumerate(item.get("items") or []))
            parts.append(f"({inner})")
        else:
            parts.append(leaf_label(item))
    text = root.join(_lower_first(p, i) for i, p in enumerate(parts))
    return text[:1].upper() + text[1:]


def _lower_first(text: str, index: int) -> str:
    """Later parts of a sentence start lower-case, except names and acronyms."""
    if index == 0 or not text or text[:3].isupper() or text.startswith(("CPU", "(")):
        return text
    return text[:1].lower() + text[1:]


# ── Judging ──────────────────────────────────────────────────────────────────

@dataclass
class LeafReading:
    """One condition's reading for one subject: ``{instance: value}`` ("" for
    a metric without instances); a value of None is unknown. ``known`` False
    means the source could not be read for this subject at all."""
    values: dict[str, Optional[float]] = field(default_factory=dict)
    known: bool = True
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class LeafResult:
    state: Optional[bool]
    matched: list[tuple[str, Optional[float]]]     # (instance, value) that are true
    shown: list[tuple[str, Optional[float]]]       # what to report when nothing matched


@dataclass
class TreeResult:
    state: Optional[bool]
    leaves: dict[str, LeafResult]
    matched: list[str]                              # ids of the conditions that made it true


def clear_condition(c: dict) -> dict:
    """The condition an open incident must leave: its clear level."""
    if c.get("metric") == "host_fs_days_left" and c.get("reset_threshold") is None:
        return {**c, "threshold": float(c["threshold"]) + 1.0}     # one day on the healthy side
    if SERVER_METRICS.get(c.get("metric"), MetricSpec("level", None)).kind == "state":
        return c
    return {**c, "threshold": nc.effective_reset_threshold(c)}


def judge_leaf(c: dict, reading: Optional[LeafReading], *, active: bool) -> LeafResult:
    cond = clear_condition(c) if active else c
    if reading is None or not reading.known:
        return LeafResult(None, [], [])
    op, thr = nc.OPS[cond["operator"]], float(cond["threshold"])
    states, matched = [], []
    for inst, value in sorted(reading.values.items()):
        state = None if value is None else bool(op(value, thr))
        states.append(state)
        if state:
            matched.append((inst, value))
    if not states:
        # Nothing to watch: no automatic service, the named service not
        # installed. That is not a breach; a source that could not be read
        # arrives as known=False instead.
        return LeafResult(False, [], [])
    state = True if True in states else (None if None in states else False)
    shown = matched or [(i, v) for i, v in sorted(reading.values.items()) if v is not None][:3]
    return LeafResult(state, matched, shown)


def judge(tree: dict, readings: dict[str, LeafReading], *, active: bool) -> TreeResult:
    """The tree's state for one subject, with what matched."""
    results = {c["id"]: judge_leaf(c, readings.get(c["id"]), active=active) for c in leaves(tree)}

    def group(items, logic):
        states, matched = [], []
        for item in items:
            if is_group(item):
                s, m = group(item.get("items") or [], item.get("logic"))
            else:
                s = results[item["id"]].state
                m = [item["id"]] if s else []
            states.append(s)
            if s:
                matched.extend(m)
        state = nc.combine(states, logic)
        return state, (matched if state else [])

    state, matched = group(tree.get("items") or [], tree.get("logic") or "AND")
    return TreeResult(state, results, matched)


@dataclass(frozen=True)
class Step:
    action: str                       # "raise" | "resolve" | "continue" | "hold" | "none"
    breach_since: Optional[datetime]
    clear_since: Optional[datetime]


def step(state: Optional[bool], *, open_incident: bool, breach_since: Optional[datetime],
         clear_since: Optional[datetime], hold_s: int, clear_hold_s: int, now: datetime) -> Step:
    """One pass of the lifecycle on the combined state.

    The breach hold counts from the first pass the tree was true; the clear
    hold from the first pass it was false with an incident open. Unknown
    restarts both clocks and changes nothing else."""
    if state is None:
        return Step("none", None, None)
    if not open_incident:
        if not state:
            return Step("none", None, None)
        since = breach_since or now
        if (now - since).total_seconds() >= hold_s:
            return Step("raise", None, None)
        return Step("hold", since, None)
    if state:
        return Step("continue", None, None)
    since = clear_since or now
    if (now - since).total_seconds() >= clear_hold_s:
        return Step("resolve", None, None)
    return Step("hold", None, since)


# ── Overrides ────────────────────────────────────────────────────────────────

def apply_override(tree: dict, override) -> dict:
    """The tree with a per-asset override's threshold / clear level on the
    condition it names (its metric), or on the first condition."""
    if override is None or (getattr(override, "threshold", None) is None
                            and getattr(override, "reset_threshold", None) is None):
        return tree
    target = next((c for c in leaves(tree)
                   if not getattr(override, "metric", None) or c["metric"] == override.metric), None)
    if target is None or SERVER_METRICS.get(target["metric"], MetricSpec("level", None)).kind == "state":
        return tree

    def swap(c):
        if c is not target:
            return c
        c = dict(c)
        if override.threshold is not None:
            c["threshold"] = float(override.threshold)
            if override.reset_threshold is None and c.get("reset_threshold") is not None:
                high = c.get("operator") in _HIGH
                if (high and c["reset_threshold"] >= c["threshold"]) or \
                        (not high and c["reset_threshold"] <= c["threshold"]):
                    c.pop("reset_threshold")
        if override.reset_threshold is not None:
            c["reset_threshold"] = float(override.reset_threshold)
        return c

    return {**tree, "items": [
        {**i, "items": [swap(c) for c in i["items"]]} if is_group(i) else swap(i)
        for i in tree["items"]]}


# ── Reporting ────────────────────────────────────────────────────────────────

def _reading_words(c: dict, instance: str, value: Optional[float], reading: Optional[LeafReading]) -> str:
    from app.services import alert_phrasing as ap
    metric = c["metric"]
    if metric == "host_service_down":
        return f"service {c.get('target')} " + ("stopped" if value else "running")
    if metric == "host_process_down":
        return f"process {c.get('target')} " + ("not running" if value else "running")
    if metric == "host_auto_service_stopped":
        detail = (reading.details.get(instance) if reading else "") or ""
        return f"automatic service {instance} " + (detail or ("stopped" if value else "running"))
    noun = ap.metric_noun(metric)
    if instance:
        noun = f"{noun} {instance}"
    if value is None:
        return f"{noun}: no current reading"
    detail = (reading.details.get(instance) if reading else "") or ""
    if metric == "host_fs_days_left" and (detail or not math.isfinite(value)):
        limit = ap.format_threshold(metric, c.get("threshold"), c.get("operator"))
        return f"{noun} {detail or 'not filling'}" + (f" (limit {limit})" if limit else "")
    shown = ap.format_reading(metric, value, c.get("operator"), c.get("threshold"))
    limit = ap.format_threshold(metric, c.get("threshold"), c.get("operator"))
    return f"{noun} {shown}" + (f" (limit {limit})" if limit else "")


def describe(tree: dict, result: TreeResult, readings: dict[str, LeafReading], *,
             matched_only: bool = True) -> str:
    """"CPU 94% (limit 90%) and memory 92% (limit 90%)": the conditions that
    made the rule true (or, with ``matched_only`` False, every condition's
    current reading, for a recovery)."""
    words = []
    for c in leaves(tree):
        if matched_only and c["id"] not in result.matched:
            continue
        lr = result.leaves.get(c["id"])
        rows = (lr.matched if matched_only else (lr.matched or lr.shown)) if lr else []
        if not rows and not matched_only:
            words.append(_reading_words(c, "", None, None) if SERVER_METRICS[c["metric"]].kind == "level"
                         else f"{leaf_label(c).lower()}: no current reading")
            continue
        for inst, value in rows[:5]:
            words.append(_reading_words(c, inst, value, readings.get(c["id"])))
        if len(rows) > 5:
            words.append(f"{len(rows) - 5} more")
    words = [_lower_first(w, i) for i, w in enumerate(words)]
    joined = ", ".join(words[:-1]) + (" and " if len(words) > 1 else "") + (words[-1] if words else "")
    return joined[:1].upper() + joined[1:] if joined else ""


def matched_metadata(tree: dict, result: TreeResult) -> list[dict]:
    """What made the rule true, as incident metadata stores it."""
    out = []
    for c in leaves(tree):
        if c["id"] not in result.matched:
            continue
        for inst, value in result.leaves[c["id"]].matched[:20]:
            out.append({"id": c["id"], "metric": c["metric"], "instance": inst or None,
                        "value": None if value is None or not math.isfinite(value) else round(value, 2)})
    return out


def states_metadata(result: TreeResult) -> dict[str, Optional[bool]]:
    return {cid: lr.state for cid, lr in result.leaves.items()}


def metrics(tree: Optional[dict]) -> set[str]:
    return {c.get("metric") for c in leaves(tree)}

