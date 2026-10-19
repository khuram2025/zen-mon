"""Authoring checks for alert rules (1.24.21): blocking errors and warnings.

Two kinds of feedback for a rule being saved (``/alert-rules``,
``/host-alert-rules``) or previewed (``POST /alert-rules/preview-warnings``):

* **Errors** (422, FastAPI's ``detail`` list shape with a ``loc`` path per
  field): a condition no reading can satisfy or that every reading satisfies
  (``network_conditions.domain_problem`` — "ping_status is 0 (down) or 1 (up);
  '> 5' can never be true"), a reset level outside the metric's range, and
  a server (host_*) metric sent to the generic rule endpoint. A ping_status
  condition that disagrees with a down/up/degraded ``trigger_on`` is not an
  error: the save rewrites it (``normalize_trigger_conditions``).
* **Warnings** (never block): the rule's name or description says something
  its condition does not — "High CPU" on a memory rule, "above 90%" with a
  threshold of 80, "High latency" with a ``<`` comparison, "Device down" on a
  rule that fires when the device comes up. Keyword heuristics, so they are
  advice only.

Pure module; no database access.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from fastapi import HTTPException

from app.services import network_conditions as nc

HOST_RULES_MESSAGE = "Server rules are edited under Servers → Alerts (host-alert-rules)"


# ── Errors ───────────────────────────────────────────────────────────────────

def error(loc: Iterable, msg: str) -> dict:
    return {"loc": list(loc), "msg": msg, "type": "value_error"}


def raise_errors(errors: list[dict]) -> None:
    """422 with every collected error (nothing when the list is empty)."""
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def is_host_metric(metric: Any) -> bool:
    return isinstance(metric, str) and metric.startswith("host_")


def host_metric_errors(metric: Any, conditions: Optional[list]) -> list[dict]:
    """A server (host_*) metric on the generic /alert-rules endpoint."""
    out = []
    if is_host_metric(metric):
        out.append(error(("body", "metric"), HOST_RULES_MESSAGE))
    for i, c in enumerate(conditions or []):
        if isinstance(c, dict) and is_host_metric(c.get("metric")):
            out.append(error(("body", "conditions", i, "metric"), HOST_RULES_MESSAGE))
    return out


def normalize_trigger_conditions(items: Optional[list], trigger_on: Any) -> tuple[list, bool]:
    """``items`` with every ping_status condition rewritten to what the
    rule's ``trigger_on`` implies (down -> ``== 0``, up/degraded -> ``== 1``),
    and whether anything changed.

    A device-status trigger decides what the rule fires on; the engine ignores
    the stored ping_status comparison for those rules. Rules saved by the old
    wizard (trigger "up" with ``ping_status == 0``) are therefore not wrong,
    only mislabelled: the save rewrites the condition instead of refusing it,
    so duplicating or editing such a row just works. ``trigger_on`` "any"
    leaves the condition alone (the value-domain checks then apply to it).
    """
    want = nc.TRIGGER_PING_STATUS.get(str(trigger_on or "any"))
    out, changed = [], False
    for c in items or []:
        if want is not None and isinstance(c, dict) and c.get("metric") == "ping_status":
            try:
                same = nc.op_symbol(c.get("operator")) == "==" and float(c.get("threshold")) == want
            except (TypeError, ValueError):
                same = False
            if not same:
                c = {**c, "operator": "==", "threshold": float(want)}
                changed = True
        out.append(c)
    return out, changed


def condition_errors(items: list[dict], *, trigger_on: Any = None, flat: bool) -> list[dict]:
    """Value-domain and reset-range errors for a rule's conditions, as they
    will be stored: a ping_status condition under a down/up/degraded trigger
    is first normalised (``normalize_trigger_conditions``), so it never
    errors. ``flat``: the single condition is the rule's own
    metric/operator/threshold (locs ``body.threshold``); otherwise each item
    is ``body.conditions[i]``."""
    out = []
    items, _ = normalize_trigger_conditions(items, trigger_on)
    for i, c in enumerate(items):
        if not isinstance(c, dict):
            continue
        base = ("body",) if flat else ("body", "conditions", i)
        metric, op, thr = c.get("metric"), c.get("operator"), c.get("threshold")
        problem = nc.domain_problem(metric, op, thr)
        if problem:
            out.append(error(base + ("threshold",), problem))
        reset = nc.reset_problem(metric, c.get("reset_threshold"))
        if reset:
            out.append(error(base + ("reset_threshold",), reset))
    return out


# ── Name / description warnings ──────────────────────────────────────────────

# Metric families a rule name may mention: (label, keyword pattern, metrics).
_FAMILIES: dict[str, tuple[str, str, frozenset]] = {
    "cpu": ("CPU", r"cpu|cpus|processor|processors",
            frozenset({"cpu", "host_cpu_pct"})),
    "memory": ("memory", r"memory|mem|ram|swap",
               frozenset({"memory", "host_memory_pct"})),
    "disk": ("disk", r"disk|disks|filesystem|filesystems|file\s+system|volume|volumes|partition|"
                     r"partitions|storage|mount|mounts",
             frozenset({"host_filesystem_pct", "host_disk_util_pct"})),
    "latency": ("latency", r"latency|rtt|round[-\s]trip|response\s+time|jitter",
                frozenset({"rtt", "jitter", "netpath_rtt", "apm_latency_p50", "apm_latency_p95",
                           "apm_latency_p99", "apm_rum_lcp_p75", "apm_rum_inp_p75"})),
    "loss": ("packet loss", r"loss|lossy",
             frozenset({"packet_loss", "netpath_loss"})),
    "temperature": ("temperature", r"temperature|temperatures|temp|thermal|overheat\w*",
                    frozenset({"temperature"})),
    "interface": ("interface", r"interface|interfaces|port|ports|link|links|uplink|uplinks|bandwidth",
                  frozenset({"if_in_bps", "if_out_bps", "if_util_pct", "if_errors", "if_discards",
                             "if_oper_status", "udt_port_capacity_pct"})),
    "reboot": ("reboot", r"reboot\w*|restart\w*|uptime",
               frozenset({"uptime_reset"})),
}
_FAMILY_RE = {k: re.compile(rf"\b(?:{pat})\b", re.I) for k, (_, pat, _) in _FAMILIES.items()}

# Metrics whose state a rule name may call "down".
_DOWN_METRICS = frozenset({
    "ping_status", "service_status", "if_oper_status", "host_service_down", "host_process_down",
    "netpath_unreachable", "apm_synthetic_down", "bgp_neighbor_down", "vpn_tunnel_state", "ha_state",
    "fan_state", "psu_state", "packet_loss", "netpath_loss",
})
_STATUS_METRICS = frozenset({"ping_status", "service_status"})

_DOWN_RE = re.compile(r"\b(?:down|offline|unreachable|unavailable|outage|not\s+responding)\b", re.I)
_UP_RE = re.compile(r"\b(?:up|recover\w*|restored|back\s+online|comes\s+online)\b", re.I)
_HIGH_RE = re.compile(r"\b(?:high|higher|above|exceed\w*|over|greater\s+than|more\s+than|spike\w*|"
                      r"too\s+many|excessive)\b", re.I)
_LOW_RE = re.compile(r"\b(?:low|lower|below|under|less\s+than|drop\w*|fall\w*|falls|insufficient)\b", re.I)
# "Low disk space", "free memory low": the words describe what is left, not the metric.
_HEADROOM_RE = re.compile(r"\b(?:free|available|remaining|headroom|space|left)\b", re.I)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

_HIGH_OPS = frozenset({">", ">="})
_LOW_OPS = frozenset({"<", "<="})


def _family_of(metric: str) -> set[str]:
    return {k for k, (_, _, metrics) in _FAMILIES.items() if metric in metrics}


def _known(metric: str) -> bool:
    return bool(_family_of(metric)) or metric in _DOWN_METRICS


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _status_state(metric: str, op: str, thr: Any, trigger_on: Any) -> Optional[str]:
    """'down' / 'up' for a status rule (what it fires on), else None."""
    trig = str(trigger_on or "any")
    if trig in ("down", "up"):
        return trig
    if trig == "degraded":
        return None
    if metric != "ping_status":
        return None
    fn = nc.OPS.get(op)
    try:
        t = float(thr)
    except (TypeError, ValueError):
        return None
    hits = {v for v in (0, 1) if fn and fn(v, t)}
    return "down" if hits == {0} else ("up" if hits == {1} else None)


def _conditions_of(rule: dict) -> list[dict]:
    conds = rule.get("conditions")
    if isinstance(conds, list) and conds:
        items = [c for c in conds if isinstance(c, dict) and c.get("metric")]
        if items:
            return [{**c, "operator": nc.op_symbol(c.get("operator"))} for c in items]
    if not rule.get("metric"):
        return []
    return [{"metric": rule.get("metric"), "operator": nc.op_symbol(rule.get("operator")),
             "threshold": rule.get("threshold"), "reset_threshold": rule.get("reset_threshold")}]


def _describe(c: dict) -> str:
    thr = c.get("threshold")
    try:
        shown = _num(float(thr))
    except (TypeError, ValueError):
        shown = str(thr)
    return f"{c.get('metric')} {c.get('operator')} {shown}"


def text_warnings(text_value: Any, field: str, conds: list[dict], trigger_on: Any) -> list[dict]:
    """Mismatches between one text field (name or description) and the conditions."""
    words = str(text_value or "").strip()
    if not words or not conds:
        return []
    out: list[dict] = []
    metrics = [str(c.get("metric") or "") for c in conds]
    families = set().union(*(_family_of(m) for m in metrics))
    all_known = all(_known(m) for m in metrics)
    checks = ", ".join(_describe(c) for c in conds)

    # 1. A metric family the text names but no condition checks.
    if all_known:
        named = [k for k, rx in _FAMILY_RE.items() if rx.search(words)]
        if all(m in _STATUS_METRICS for m in metrics):
            # "WAN link down" on a ping rule is ordinary wording.
            named = [k for k in named if k != "interface"]
        missing = [k for k in named if k not in families]
        # Several families named and one of them checked ("CPU and memory"
        # on a CPU rule) is not flagged.
        if missing and not (set(named) & families):
            labels = " and ".join(_FAMILIES[k][0] for k in missing)
            out.append({"field": field,
                        "message": f"The {field} mentions {labels}, but the rule checks {checks}."})

    # 2. Down / up wording against what a status rule fires on.
    status = [c for c in conds if c["metric"] in _STATUS_METRICS]
    says_down, says_up = bool(_DOWN_RE.search(words)), bool(_UP_RE.search(words))
    if status:
        state = _status_state(status[0]["metric"], status[0]["operator"], status[0].get("threshold"),
                              trigger_on)
        if says_down and not says_up and state == "up":
            out.append({"field": field, "message": f"The {field} says down, but the rule fires when the "
                                                   f"device comes up ({checks}, trigger {trigger_on or 'any'})."})
        elif says_up and not says_down and state == "down":
            out.append({"field": field, "message": f"The {field} says up/recovered, but the rule fires when "
                                                   f"the device goes down ({checks})."})
    elif says_down and all_known and not any(m in _DOWN_METRICS for m in metrics):
        if all(c["operator"] in _HIGH_OPS for c in conds):
            out.append({"field": field,
                        "message": f"The {field} says down/offline, but the rule checks {checks}."})

    # 3. Direction words against the comparison of numeric conditions.
    numeric = [c for c in conds if c["metric"] in nc.VALUE_DOMAINS
               and not nc.VALUE_DOMAINS[c["metric"]].values]
    high, low = bool(_HIGH_RE.search(words)), bool(_LOW_RE.search(words))
    if numeric and high != low and not _HEADROOM_RE.search(words):
        ops = {c["operator"] for c in numeric}
        if high and ops <= _LOW_OPS:
            out.append({"field": field, "message": f"The {field} says high/above, but the condition fires "
                                                   f"below the threshold ({checks})."})
        elif low and ops <= _HIGH_OPS:
            out.append({"field": field, "message": f"The {field} says low/below, but the condition fires "
                                                   f"above the threshold ({checks})."})

    # 4. "N%" in the text against a percentage threshold.
    pct = [float(m) for m in _PERCENT_RE.findall(words)]
    pct_conds = [c for c in conds if c["metric"] in nc._PERCENT]
    if len(set(pct)) == 1 and len(pct_conds) == 1:
        c = pct_conds[0]
        said = pct[0]
        levels = []
        for key in ("threshold", "reset_threshold"):
            try:
                levels.append(float(c.get(key)))
            except (TypeError, ValueError):
                pass
        if levels and all(abs(said - v) > 1e-9 for v in levels):
            out.append({"field": field, "message": f"The {field} says {_num(said)}%, but the threshold is "
                                                   f"{_num(levels[0])} ({checks})."})
    return out


def name_warnings(rule: dict) -> list[dict]:
    """Non-blocking ``[{field, message}]`` for a rule payload (name,
    description, metric/operator/threshold or conditions, trigger_on)."""
    conds = _conditions_of(rule)
    out = text_warnings(rule.get("name"), "name", conds, rule.get("trigger_on"))
    out += text_warnings(rule.get("description"), "description", conds, rule.get("trigger_on"))
    seen, unique = set(), []
    for w in out:
        key = (w["field"], w["message"])
        if key not in seen:
            seen.add(key)
            unique.append(w)
    return unique
