"""Which template a device belongs to: the classification contract.

The poller (``poller/internal/checker/snmp/profile.go``) and this module
implement the same rules and both pass
``scripts/dev/template-match-contract/vectors.json``:

* OIDs are normalised (leading dot, ``iso.`` form) and a prefix matches only
  on a dot boundary: ``1.3.6.1.4.1.9`` matches ``1.3.6.1.4.1.9.1.5`` but not
  ``1.3.6.1.4.1.99``.
* A template matches by sysObjectID prefix (strength 2) or, when none of its
  prefixes matched, by ``sys_descr_regex`` (strength 1). With
  ``require_sys_descr`` a prefix hit only counts when the regex matches too.
* Candidates rank by ``match_priority`` (higher first), then strength, then the
  longest matching prefix, then name in byte order. Ties cannot occur.
* An invalid regular expression never matches and never raises.

Pure: no database access, so preview, explain and tests share one function.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Optional

from app.core.snmp_oid import normalize_oid

MAX_REGEX_LEN = 512

# Constructs Python accepts but Go's RE2 (the poller) rejects: a pattern that
# uses them would match here and never in the poller.
_RE2_UNSUPPORTED = (
    (re.compile(r"\(\?[=!]"), "lookahead (?= / (?!"),
    (re.compile(r"\(\?<[=!]"), "lookbehind (?<= / (?<!"),
    (re.compile(r"\(\?P="), "named backreference (?P=…)"),
    (re.compile(r"\\[1-9]"), r"backreference \1…\9"),
    (re.compile(r"\(\?>"), "atomic group (?>"),
    (re.compile(r"(?<!\\)[*+?}]\+"), "possessive quantifier"),
    (re.compile(r"\(\?\("), "conditional (?(…)"),
)


def regex_problem(pattern: Optional[str]) -> Optional[str]:
    """Why ``pattern`` cannot be saved, or None when it is usable by both engines."""
    if pattern is None or pattern == "":
        return None
    if len(pattern) > MAX_REGEX_LEN:
        return f"longer than {MAX_REGEX_LEN} characters"
    for probe, label in _RE2_UNSUPPORTED:
        if probe.search(pattern):
            return f"uses {label}, which the poller's regex engine does not support"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid regular expression: {exc}"
    return None


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> Optional[re.Pattern]:
    if regex_problem(pattern):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _descr_matches(pattern: Optional[str], sys_descr: str) -> bool:
    if not pattern:
        return False
    rx = _compiled(pattern)
    return bool(rx and rx.search(sys_descr or ""))


def prefix_matches(oid: str, prefix: str) -> bool:
    oid, prefix = normalize_oid(oid), normalize_oid(prefix)
    return bool(oid and prefix) and (oid == prefix or oid.startswith(prefix + "."))


@dataclass
class Candidate:
    template: Any
    priority: int
    strength: int            # 2 = sysObjectID prefix, 1 = sysDescr only
    prefix: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple:
        return (-self.priority, -self.strength, -len(self.prefix), _name(self.template).encode())


def _get(t: Any, key: str, default=None):
    return t.get(key, default) if isinstance(t, dict) else getattr(t, key, default)


def _name(t: Any) -> str:
    return str(_get(t, "name", "") or "")


def evaluate(template: Any, sys_object_id: str, sys_descr: str) -> tuple[Optional[Candidate], list[str]]:
    """Whether one template matches, with the reason for every test."""
    rules = _get(template, "match_rules") or {}
    priority = int(_get(template, "match_priority", 100) if _get(template, "match_priority") is not None else 100)
    prefixes = [normalize_oid(p) for p in (rules.get("sys_object_id_prefixes") or []) if normalize_oid(p)]
    pattern = rules.get("sys_descr_regex") or None
    require = bool(rules.get("require_sys_descr"))
    notes: list[str] = []

    if pattern and _compiled(pattern) is None:
        notes.append("sysDescr pattern is not usable and never matches")
    descr_ok = _descr_matches(pattern, sys_descr)
    hits = [p for p in prefixes if prefix_matches(sys_object_id, p)]
    if hits:
        best = max(hits, key=len)
        if require and not descr_ok:
            notes.append(f"sysObjectID matches {best} but sysDescr does not match the required pattern")
            return None, notes
        notes.append(f"sysObjectID matches prefix {best}")
        return Candidate(template, priority, 2, best, notes), notes
    if prefixes:
        notes.append("no sysObjectID prefix matches")
    if pattern:
        if descr_ok:
            notes.append("sysDescr matches the pattern")
            return Candidate(template, priority, 1, "", notes), notes
        notes.append("sysDescr does not match the pattern")
    if not prefixes and not pattern:
        notes.append("no match rules: assigned by hand only")
    return None, notes


def rank(templates: Iterable[Any], sys_object_id: str, sys_descr: str) -> list[Candidate]:
    """Every matching template, best first."""
    out = [c for c in (evaluate(t, sys_object_id, sys_descr)[0] for t in templates) if c is not None]
    out.sort(key=lambda c: c.sort_key)
    return out


def match(templates: Iterable[Any], sys_object_id: str, sys_descr: str) -> Optional[Any]:
    ranked = rank(templates, sys_object_id, sys_descr)
    return ranked[0].template if ranked else None


def explain(templates: Iterable[Any], sys_object_id: str, sys_descr: str) -> dict:
    """Every template with whether it matched and why, plus the winner."""
    rows = []
    winners: list[Candidate] = []
    for t in templates:
        cand, notes = evaluate(t, sys_object_id, sys_descr)
        rows.append({"id": str(_get(t, "id", "")), "name": _name(t),
                     "match_priority": _get(t, "match_priority", 100),
                     "matched": cand is not None,
                     "strength": cand.strength if cand else 0,
                     "prefix": cand.prefix if cand else None,
                     "reasons": notes})
        if cand:
            winners.append(cand)
    winners.sort(key=lambda c: c.sort_key)
    order = {str(_get(c.template, "id", "")): i + 1 for i, c in enumerate(winners)}
    for r in rows:
        r["rank"] = order.get(r["id"])
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["name"]))
    best = winners[0].template if winners else None
    return {"sys_object_id": normalize_oid(sys_object_id), "sys_descr": sys_descr or "",
            "winner": ({"id": str(_get(best, "id", "")), "name": _name(best)} if best is not None else None),
            "candidates": rows}


def extract(template: Any, sys_descr: str) -> tuple[str, str, str]:
    """Vendor, model and OS version read from sysDescr by the template's rules."""
    rules = _get(template, "match_rules") or {}

    def first(pattern: Optional[str]) -> str:
        rx = _compiled(pattern) if pattern else None
        m = rx.search(sys_descr or "") if rx else None
        if not m:
            return ""
        for v in list(m.groupdict().values()) + list(m.groups()):
            if v:
                return v.strip()
        return ""

    vendor = first(rules.get("extract_vendor")) or rules.get("default_vendor") or _get(template, "vendor") or ""
    model = first(rules.get("extract_model")) or rules.get("default_model") or ""
    return vendor, model, first(rules.get("extract_os_version"))
