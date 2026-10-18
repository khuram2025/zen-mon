"""Incident priority (P1..P4) beside severity — the escalation-matrix contract.

Severity says how bad the measured condition is (``info|warning|critical``);
priority says how urgently a human must respond. They are separate because a
"critical CPU" threshold on one server is not a major outage:

* ``P1`` Critical — confirmed major outage. Only ever chosen explicitly on a
  rule (``alert_rules.priority = 'P1'``); severity alone never derives it.
* ``P2`` High — derived from ``critical`` severity.
* ``P3`` Warning — derived from ``warning`` severity.
* ``P4`` Information — derived from ``info`` severity.

``alert_rules.priority`` NULL means "derive from severity"; ``alerts.priority``
is stamped once when the incident is raised (every raise path calls
:func:`priority_for`), so a later rule edit does not silently re-rank open
incidents.

Pure module: no database or I/O, safe to import from any evaluator.
"""

from __future__ import annotations

from typing import Any

PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3", "P4")

PRIORITY_LABELS: dict[str, str] = {
    "P1": "Critical",
    "P2": "High",
    "P3": "Warning",
    "P4": "Information",
}

# Severity -> priority when a rule names none. P1 is never derived.
SEVERITY_PRIORITY: dict[str, str] = {
    "critical": "P2",
    "warning": "P3",
    "info": "P4",
}

DEFAULT_PRIORITY = "P3"

# Acknowledgement targets (minutes) per priority; None = no target.
# Matches the field-report matrix: P1 5 min, P2 15 min, P3 60 min, P4 none.
DEFAULT_RESPONSE_TARGETS: dict[str, int | None] = {"P1": 5, "P2": 15, "P3": 60, "P4": None}

# Legacy severity-keyed targets (pre-1.24.17 ``alert_response_targets``).
LEGACY_TARGET_KEYS: tuple[str, ...] = ("critical", "warning", "info")


def normalize_priority(value: Any) -> str | None:
    """``'p2'``/``'P2'``/``2`` -> ``'P2'``; anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = f"P{value}"
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if v in PRIORITIES:
        return v
    return None


def is_priority(value: Any) -> bool:
    return isinstance(value, str) and value in PRIORITIES


def severity_priority(severity: Any) -> str:
    """Priority derived from severity alone (critical->P2, warning->P3, info->P4)."""
    sev = str(severity or "").strip().lower()
    return SEVERITY_PRIORITY.get(sev, DEFAULT_PRIORITY)


def priority_for(rule_priority: Any, severity: Any) -> str:
    """The priority to stamp on a newly raised alert.

    A rule's explicit priority wins; otherwise it is derived from the alert's
    severity. Always returns one of :data:`PRIORITIES`.
    """
    explicit = normalize_priority(rule_priority)
    if explicit:
        return explicit
    return severity_priority(severity)


def effective_priority(alert_priority: Any, rule_priority: Any = None, severity: Any = None) -> str:
    """Priority of an existing alert row: its stamp, else derived (rows raised
    before migration 164 carry no stamp)."""
    stamped = normalize_priority(alert_priority)
    if stamped:
        return stamped
    return priority_for(rule_priority, severity)


def priority_label(priority: Any) -> str:
    """``'P2'`` -> ``'P2 High'``; unknown -> ``''``."""
    p = normalize_priority(priority)
    if not p:
        return ""
    return f"{p} {PRIORITY_LABELS[p]}"


def priority_rank(priority: Any) -> int:
    """Lower is more urgent (P1 -> 1). Unknown sorts after P4."""
    p = normalize_priority(priority)
    return PRIORITIES.index(p) + 1 if p else len(PRIORITIES) + 1


def is_urgent(priority: Any) -> bool:
    """P1/P2 notifications are never held, grouped or budgeted."""
    return normalize_priority(priority) in ("P1", "P2")


def _minutes(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v > 0 else None


def clean_response_targets(raw: Any) -> dict[str, int | None]:
    """Normalize a stored/posted targets dict to P1..P4 keys.

    P-keys win. A priority without a P-key reads the legacy severity key that
    derives to it (critical->P2, warning->P3, info->P4) so settings saved by
    earlier releases keep their meaning. P1 has no legacy key and keeps its
    default unless given explicitly.
    """
    out: dict[str, int | None] = dict(DEFAULT_RESPONSE_TARGETS)
    if not isinstance(raw, dict):
        return out
    for sev in LEGACY_TARGET_KEYS:
        if sev in raw:
            out[SEVERITY_PRIORITY[sev]] = _minutes(raw[sev])
    for p in PRIORITIES:
        if p in raw:
            out[p] = _minutes(raw[p])
        elif p.lower() in raw:
            out[p] = _minutes(raw[p.lower()])
    return out


def response_target_minutes(targets: dict | None, priority: Any) -> int | None:
    """Acknowledgement target for ``priority`` from a targets dict.

    Reads the P-key first; when absent, falls back to the legacy severity key
    that derives to that priority. Returns None when no target applies.
    """
    p = normalize_priority(priority) or DEFAULT_PRIORITY
    t = targets or {}
    if p in t:
        return _minutes(t[p])
    for sev, derived in SEVERITY_PRIORITY.items():
        if derived == p and sev in t:
            return _minutes(t[sev])
    return DEFAULT_RESPONSE_TARGETS.get(p)


def subject_tag(priority: Any) -> str:
    """``'[P3]'`` for subjects; ``''`` when unknown."""
    p = normalize_priority(priority)
    return f"[{p}]" if p else ""


def severity_priority_sql(severity_expr: str) -> str:
    """SQL twin of :func:`severity_priority` for a severity expression."""
    return (f"(CASE lower({severity_expr}) WHEN 'critical' THEN 'P2' WHEN 'warning' THEN 'P3'"
            f" WHEN 'info' THEN 'P4' ELSE '{DEFAULT_PRIORITY}' END)")


def priority_sql(a: str = "a", r: str | None = "r", *, columns: bool = True) -> str:
    """SQL twin of :func:`effective_priority` for alert alias ``a`` (and rule
    alias ``r`` when joined). ``columns=False`` derives from severity only, for
    databases where migration 164 (the priority columns) has not run yet."""
    derived = severity_priority_sql(f"{a}.severity")
    if not columns:
        return derived
    rule = f"{r}.priority, " if r else ""
    return f"COALESCE({a}.priority, {rule}{derived})"
