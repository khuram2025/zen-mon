"""State (enum) metrics of device templates, seen from the alerting side.

A template's enum metric (``type: "enum"``) carries a value map: ``labels``
maps each code to ``{text, sev}`` (sev ok, info, warn or crit), and string
enums may add ``value_map`` (device string -> code). Alert rules compare the
code with the ordinary numeric condition model (``tpl_<key> == 1``,
``tpl_<key> >= 3`` AND ``!= 5``); this module reads the value map to

* tell which codes are *bad* (:func:`bad_codes`): the codes labelled crit,
  or, when a metric has no crit code, every code other than its single
  healthy one when all of those are labelled warn (a BGP peer that is not
  Established);
* express that set as at most three numeric conditions on the same metric
  (:func:`conditions_for`), AND-combined, so a recommended state rule is an
  ordinary rule the wizard can show and edit;
* phrase a state incident with the template's names (:func:`describe`): the
  component (row label), the metric's display name and the value's label,
  never the series key or the raw code.

Groups whose rows are controller-managed children (``display: "children"``,
``belongs_to`` or a ``children`` mapping: FortiAPs, FortiSwitches) belong to
the children, not to the controller (docs/controller-managed-children.md);
:func:`child_scoped` tells them apart so the controller's template never
recommends rules on them.

No template definition is changed here: this only reads ``oid_groups``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

STATE_HOLD_S = 120
MAX_CONDITIONS = 4
_BAD_SEV = "crit"
_GENERIC_NAMES = {"status", "state"}

# Tokens of "<group key> <metric key>" that make a state failure critical (P2):
# power, HA / failover / config sync, BGP, a red chassis alarm.
_CRITICAL_TOKENS = {"psu", "psus", "power", "ha", "failover", "sync", "bgp"}


@dataclass(frozen=True)
class MetricInfo:
    key: str
    name: str
    type: str
    group_key: str
    group_name: str
    group_kind: str
    child: bool
    labels: dict = field(default_factory=dict)       # {code(int): (text, sev)}
    value_names: dict = field(default_factory=dict)  # {code(int): device string}

    @property
    def is_state(self) -> bool:
        return self.type == "enum" and bool(self.labels or self.value_names)

    def label(self, value) -> Optional[str]:
        """The value's label ("Down"), or None when the code is unknown."""
        code = _code(value)
        if code is None:
            return None
        if code in self.labels:
            return self.labels[code][0]
        return self.value_names.get(code)

    def known_codes(self) -> list[int]:
        return sorted(set(self.labels) | set(self.value_names))


def _code(value) -> Optional[int]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f != int(f):
        return None
    return int(f)


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def child_scoped(group: dict) -> bool:
    """Rows of this group are (or belong to) controller-managed children."""
    return (group.get("display") == "children" or bool(group.get("belongs_to"))
            or isinstance(group.get("children"), dict))


def metric_index(groups) -> dict[str, MetricInfo]:
    """{metric key: MetricInfo} for a template's oid_groups (JSON or list)."""
    out: dict[str, MetricInfo] = {}
    for g in _json(groups) or []:
        if not isinstance(g, dict):
            continue
        child = child_scoped(g)
        for m in g.get("metrics") or []:
            if not isinstance(m, dict) or not m.get("key"):
                continue
            labels = {}
            for code, spec in (m.get("labels") or {}).items():
                c = _code(code)
                if c is None:
                    continue
                if isinstance(spec, dict):
                    labels[c] = (str(spec.get("text") or code), str(spec.get("sev") or ""))
                else:
                    labels[c] = (str(spec), "")
            names = {}
            for text_, code in (m.get("value_map") or {}).items():
                c = _code(code)
                if c is not None:
                    names[c] = str(text_)
            out[m["key"]] = MetricInfo(
                key=m["key"], name=str(m.get("name") or m["key"]), type=str(m.get("type") or "gauge"),
                group_key=str(g.get("key") or ""), group_name=str(g.get("name") or g.get("key") or ""),
                group_kind=str(g.get("kind") or "scalar"), child=child, labels=labels, value_names=names)
    return out


def bad_codes(info: MetricInfo) -> list[int]:
    """The codes that unambiguously mean a fault, or [] when there are none."""
    if not info.is_state:
        return []
    crit = sorted(c for c, (_, sev) in info.labels.items() if sev == _BAD_SEV)
    if crit:
        return crit
    ok = [c for c, (_, sev) in info.labels.items() if sev == "ok"]
    rest = [c for c, (_, sev) in info.labels.items() if sev != "ok"]
    if len(ok) == 1 and rest and all(info.labels[c][1] == "warn" for c in rest):
        return sorted(rest)
    return []


def conditions_for(metric: str, info: MetricInfo, bad: list[int]) -> Optional[list[dict]]:
    """At most MAX_CONDITIONS AND-combined numeric conditions that breach exactly on
    the bad codes among the known ones; None when that takes more."""
    codes = info.known_codes()
    bad = sorted(set(bad))
    if not bad or not codes:
        return None
    if len(bad) == 1:
        return [{"metric": metric, "operator": "==", "threshold": float(bad[0])}]
    good = [c for c in codes if c not in bad]
    if len(good) == 1:
        return [{"metric": metric, "operator": "!=", "threshold": float(good[0])}]
    lo, hi = bad[0], bad[-1]
    out = []
    if lo > codes[0]:
        out.append({"metric": metric, "operator": ">=", "threshold": float(lo)})
    if hi < codes[-1]:
        out.append({"metric": metric, "operator": "<=", "threshold": float(hi)})
    out += [{"metric": metric, "operator": "!=", "threshold": float(c)}
            for c in codes if lo <= c <= hi and c not in bad]
    return out if 0 < len(out) <= MAX_CONDITIONS else None


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


def classify(info: MetricInfo) -> tuple[str, str]:
    """(severity, priority) of a recommended state rule: critical P2 for power,
    HA, config sync, BGP and a red alarm; warning P3 otherwise (a tunnel, a fan)."""
    tokens = set(f"{info.group_key}_{info.key}".lower().split("_")) | set(info.name.lower().split())
    if tokens & _CRITICAL_TOKENS or ("alarm" in tokens and "red" in tokens):
        return "critical", "P2"
    return "warning", "P3"


def recommendations(groups) -> list[dict]:
    """Recommended state rules: one per enum metric with an unambiguous bad state,
    outside controller-managed child groups. Recommendations only."""
    out = []
    for key, info in metric_index(groups).items():
        if info.child or not info.is_state:
            continue
        bad = bad_codes(info)
        metric = f"tpl_{key}"
        conds = conditions_for(metric, info, bad)
        if not conds:
            continue
        if len(conds) == 1 and conds[0]["operator"] == "!=":
            # Everything but the one healthy state: "BGP Peers: State is not Established".
            words = f"not {info.label(conds[0]['threshold'])}"
        else:
            words = _join([info.label(c) or str(c) for c in bad])
        base = f"{info.group_name}: {info.name}" if info.group_kind == "table" or \
            info.name.lower() in _GENERIC_NAMES else info.name
        sev, prio = classify(info)
        each = "each row" if info.group_kind == "table" else "the device"
        out.append({
            "key": f"{key}:state",
            "name": f"{base} is {words}",
            "description": (f"Recommended from the template's states for {base}: alerts when {each} "
                            f"reports {words} for 2 minutes, one incident per "
                            f"{'row' if info.group_kind == 'table' else 'device'}."),
            "metric": metric, "operator": conds[0]["operator"], "threshold": conds[0]["threshold"],
            "conditions": conds, "states": bad,
            "condition_label": f"{info.name} is {words}",
            "reset_threshold": None, "min_duration": STATE_HOLD_S, "clear_duration": None,
            "severity": sev, "priority": prio, "if_role": None, "source": "state",
        })
    return out


def child_metric_keys(groups) -> set[str]:
    """Metric keys of controller-managed child groups."""
    return {k for k, info in metric_index(groups).items() if info.child}


# ─── Wording ─────────────────────────────────────────────────────────────────

def component_noun(group_name: str) -> str:
    """"IPsec Tunnels" -> "IPsec tunnel", "Power Supplies" -> "Power supply"."""
    words = (group_name or "").split()
    if not words:
        return ""
    last = words[-1]
    if last.lower().endswith("ies") and len(last) > 3:
        last = last[:-3] + ("Y" if last[-3].isupper() else "y")
    elif last.lower().endswith("s") and not last.lower().endswith("ss") and len(last) > 1:
        last = last[:-1]
    words[-1] = last
    out = [words[0]] + [w if (w.isupper() or any(ch.isupper() for ch in w[1:])) else w.lower()
                        for w in words[1:]]
    return " ".join(out)


def component_label(info: Optional[MetricInfo], instance_label: Optional[str]) -> Optional[str]:
    """"IPsec tunnel Branch-12": the row as an operator names it (None for a device value)."""
    inst = (instance_label or "").strip()
    if not inst:
        return None
    noun = component_noun(info.group_name) if info else ""
    if not noun or inst.lower().startswith(noun.lower().split()[-1].lower()):
        return inst
    return f"{noun} {inst}"


def describe(info: MetricInfo, value, *, component: Optional[str], who: str,
             recovered: bool = False, bad_label: Optional[str] = None) -> dict:
    """Sentence and parts for one state reading.

    "IPsec tunnel Branch-12 on Firewall FGT-01 (10.0.0.1) is Down."
    "HA member FGT-B on Firewall FGT-01 (10.0.0.1): Sync Status is Not Synchronized."
    Recovery: "... is Up again." A code the template does not know reads
    "an unknown state (code 7)"; a missing reading never reads as healthy.
    """
    label = info.label(value)
    code = _code(value)
    if label is None:
        label = f"an unknown state (code {code})" if code is not None else "unknown (no current reading)"
    generic = info.name.lower() in _GENERIC_NAMES
    if component:
        subject = f"{component} on {who}" if generic else f"{component} on {who}: {info.name}"
    else:
        subject = f"{info.name} on {who}"
    if recovered:
        if info.label(value) is not None:
            sentence = f"{subject} is {label} again"
        else:
            sentence = f"{subject} is no longer {bad_label or 'in a fault state'}"
    else:
        sentence = f"{subject} is {label}"
    return {"sentence": sentence[:1].upper() + sentence[1:] + ".", "metric_name": info.name,
            "value_label": label, "component": component}
