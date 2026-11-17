"""Condition trees: validation and a pure evaluator that explains itself.

A condition is either a group or a leaf::

    {"op": "all" | "any" | "none", "conditions": [ ... ]}
    {"field": "sys_object_id", "operator": "starts_with", "value": "1.3.6.1.4.1.12356"}

``all`` is AND, ``any`` is OR, ``none`` is NOT (none of the children match).
Groups nest at most ``MAX_DEPTH`` levels; a tree holds at most ``MAX_LEAVES``
leaves. An empty ``all`` group matches everything (a catch-all rule); an
empty ``any`` group matches nothing.

Missing facts (unknown vendor, no sysObjectID, ...) never satisfy a positive
test. Negative operators (``not_equals``, ``not_contains``, ``not_in_subnet``,
``has_none``, ``is_not``) are true for a missing value: an unknown vendor is
"not Fortinet". ``is_empty`` / ``is_not_empty`` test for the missing value
itself. Preview shows the effect before a rule is activated.

Text comparisons ignore case. Regular expressions are validated when the rule
is saved (length limit, no nested quantifiers) and run against at most
``MAX_SUBJECT`` characters, because the standard library ``re`` has no time
limit of its own.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import date
from functools import lru_cache
from typing import Any

from app.core.snmp_oid import normalize_oid

from .facts import ENUM_VALUES, FIELDS

MAX_DEPTH = 3
MAX_LEAVES = 50
MAX_LIST = 200
MAX_REGEX = 200
MAX_SUBJECT = 2048

GROUP_OPS = ("all", "any", "none")

# type -> {operator: (label, value kind)}; value kind is what the builder asks
# for: "none", "text", "list", "number", "range", "cidr_list", "ip_range", "ref", "ref_list"
OPERATORS: dict[str, dict[str, tuple[str, str]]] = {
    "text": {
        "equals": ("equals", "text"),
        "not_equals": ("does not equal", "text"),
        "contains": ("contains", "text"),
        "not_contains": ("does not contain", "text"),
        "starts_with": ("starts with", "text"),
        "ends_with": ("ends with", "text"),
        "matches": ("matches regex", "text"),
        "in_list": ("is one of", "list"),
        "is_empty": ("is empty", "none"),
        "is_not_empty": ("is not empty", "none"),
    },
    "oid": {
        "equals": ("equals", "text"),
        "starts_with": ("is under (OID subtree)", "text"),
        "not_starts_with": ("is not under (OID subtree)", "text"),
        "in_list": ("is one of", "list"),
        "matches": ("matches regex", "text"),
        "is_empty": ("is empty", "none"),
        "is_not_empty": ("is not empty", "none"),
    },
    "version": {
        "equals": ("equals", "text"),
        "contains": ("contains", "text"),
        "starts_with": ("starts with", "text"),
        "matches": ("matches regex", "text"),
        "version_lt": ("is older than", "text"),
        "version_lte": ("is at most", "text"),
        "version_gt": ("is newer than", "text"),
        "version_gte": ("is at least", "text"),
        "is_empty": ("is empty", "none"),
        "is_not_empty": ("is not empty", "none"),
    },
    "ip": {
        "in_subnet": ("is in subnet", "cidr_list"),
        "not_in_subnet": ("is not in subnet", "cidr_list"),
        "in_range": ("is in range", "ip_range"),
        "equals": ("equals", "text"),
        "in_list": ("is one of", "list"),
    },
    "number": {
        "eq": ("=", "number"), "ne": ("≠", "number"),
        "lt": ("<", "number"), "lte": ("≤", "number"),
        "gt": (">", "number"), "gte": ("≥", "number"),
        "between": ("is between", "range"),
    },
    "set": {
        "has_any": ("has any of", "list"),
        "has_all": ("has all of", "list"),
        "has_none": ("has none of", "list"),
        "is_empty": ("is empty", "none"),
        "is_not_empty": ("is not empty", "none"),
    },
    "enum": {
        "is": ("is", "text"),
        "is_not": ("is not", "text"),
        "is_any_of": ("is any of", "list"),
    },
    "reference": {
        "is": ("is", "ref"),
        "is_not": ("is not", "ref"),
        "is_any_of": ("is any of", "ref_list"),
        "is_empty": ("is empty", "none"),
    },
    "boolean": {
        "is_true": ("is true", "none"),
        "is_false": ("is false", "none"),
    },
    # Dates (custom fields): ISO YYYY-MM-DD values.
    "date": {
        "on": ("is on", "text"),
        "before": ("is before", "text"),
        "after": ("is after", "text"),
        "is_empty": ("is empty", "none"),
        "is_not_empty": ("is not empty", "none"),
    },
}

NEGATIVE = frozenset({"not_equals", "not_contains", "not_starts_with", "not_in_subnet",
                      "has_none", "is_not", "is_empty"})


class ConditionError(ValueError):
    """A condition tree that cannot be saved; the message names the problem."""


# ── validation ──────────────────────────────────────────────────────────────

_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


def validate_regex(pattern: str) -> None:
    if not isinstance(pattern, str) or not pattern:
        raise ConditionError("A regular expression is required")
    if len(pattern) > MAX_REGEX:
        raise ConditionError(f"Regular expressions are limited to {MAX_REGEX} characters")
    if _NESTED_QUANTIFIER.search(pattern) or re.search(r"\\[1-9]", pattern):
        raise ConditionError(
            "Nested quantifiers such as (a+)+ and back-references are not allowed: they can make "
            "matching take very long")
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConditionError(f"Invalid regular expression: {e}") from None


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [v.strip() for v in str(value).replace("\n", ",").split(",") if v.strip()]


def _validate_leaf(node: dict, fields: dict | None = None, enums: dict | None = None) -> dict:
    FIELDS_ = fields if fields is not None else FIELDS
    ENUMS_ = enums if enums is not None else ENUM_VALUES
    return _validate_leaf_in(node, FIELDS_, ENUMS_)


def _validate_leaf_in(node: dict, FIELDS: dict, ENUM_VALUES: dict) -> dict:  # noqa: N803 (shadows the defaults)
    field = node.get("field")
    if field not in FIELDS:
        raise ConditionError(f"Unknown field: {field!r}")
    ftype = FIELDS[field][0]
    op = node.get("operator")
    ops = OPERATORS[ftype]
    if op not in ops:
        raise ConditionError(f"Operator {op!r} cannot be used with {FIELDS[field][1]}")
    kind = ops[op][1]
    value = node.get("value")
    if kind == "none":
        value = None
    elif kind in ("list", "cidr_list", "ref_list"):
        value = [str(v).strip() for v in _as_list(value) if str(v).strip()]
        if not value:
            raise ConditionError(f"{FIELDS[field][1]} {ops[op][0]}: give at least one value")
        if len(value) > MAX_LIST:
            raise ConditionError(f"Lists are limited to {MAX_LIST} values")
        if kind == "cidr_list":
            for v in value:
                try:
                    ipaddress.ip_network(v, strict=False)
                except ValueError:
                    raise ConditionError(f"Not a subnet: {v!r}") from None
        if ftype == "set" and field == "open_ports":
            for v in value:
                if not v.isdigit() or not 0 < int(v) < 65536:
                    raise ConditionError(f"Not a TCP port: {v!r}")
        if ftype == "enum":
            bad = [v for v in value if v not in ENUM_VALUES.get(field, [])]
            if bad:
                raise ConditionError(f"Unknown {FIELDS[field][1]} value: {bad[0]!r}")
    elif kind == "number":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ConditionError(f"{FIELDS[field][1]}: a number is required") from None
    elif kind == "range":
        vals = _as_list(value)
        try:
            lo, hi = float(vals[0]), float(vals[1])
        except (IndexError, TypeError, ValueError):
            raise ConditionError(f"{FIELDS[field][1]} is between: give two numbers") from None
        value = [min(lo, hi), max(lo, hi)]
    elif kind == "ip_range":
        try:
            a, b = _ip_range(str(value))
        except ValueError:
            raise ConditionError(f"Not an address range: {value!r} (use 10.0.0.1-10.0.0.99)") from None
        value = f"{a}-{b}"
    else:  # text / ref
        if value is None or str(value).strip() == "":
            raise ConditionError(f"{FIELDS[field][1]} {ops[op][0]}: a value is required")
        value = str(value).strip()
        if len(value) > 500:
            raise ConditionError("Values are limited to 500 characters")
        if op == "matches":
            validate_regex(value)
        if ftype == "ip" and op == "equals":
            try:
                value = str(ipaddress.ip_address(value))
            except ValueError:
                raise ConditionError(f"Not an IP address: {value!r}") from None
        if ftype == "enum" and value not in ENUM_VALUES.get(field, []):
            raise ConditionError(f"Unknown {FIELDS[field][1]} value: {value!r}")
        if ftype == "oid" and op != "matches":
            value = normalize_oid(value)
        if ftype == "date":
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                raise ConditionError(f"{FIELDS[field][1]}: give a date as YYYY-MM-DD") from None
    return {"field": field, "operator": op, "value": value}


def validate(node: Any, *, _depth: int = 1, _count: list | None = None,
             fields: dict | None = None, enums: dict | None = None) -> dict:
    """Normalised copy of a condition tree, or ``ConditionError``. ``fields`` /
    ``enums`` replace the discovery-result catalog (device groups test the facts
    of existing devices and servers)."""
    count = _count if _count is not None else [0]
    if not isinstance(node, dict):
        raise ConditionError("A condition must be an object")
    if "op" in node:
        op = node.get("op")
        if op not in GROUP_OPS:
            raise ConditionError(f"Unknown group operator: {op!r} (all, any or none)")
        if _depth > MAX_DEPTH:
            raise ConditionError(f"Groups nest at most {MAX_DEPTH} levels deep")
        children = node.get("conditions") or []
        if not isinstance(children, list):
            raise ConditionError("A group's conditions must be a list")
        return {"op": op, "conditions": [validate(c, _depth=_depth + 1, _count=count, fields=fields, enums=enums)
                                         for c in children]}
    count[0] += 1
    if count[0] > MAX_LEAVES:
        raise ConditionError(f"A rule may test at most {MAX_LEAVES} conditions")
    return _validate_leaf(node, fields, enums)


def fields_used(node: dict) -> set[str]:
    if "op" in node:
        out: set[str] = set()
        for c in node.get("conditions") or []:
            out |= fields_used(c)
        return out
    return {node.get("field")}


# ── evaluation ──────────────────────────────────────────────────────────────

def _ip_range(value: str):
    left, _, right = value.partition("-")
    a = ipaddress.ip_address(left.strip())
    b = ipaddress.ip_address(right.strip()) if right.strip() else a
    if a.version != b.version:
        raise ValueError("mixed families")
    return (a, b) if int(a) <= int(b) else (b, a)


@lru_cache(maxsize=512)
def _regex(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


@lru_cache(maxsize=512)
def _networks(values: tuple) -> tuple:
    return tuple(ipaddress.ip_network(v, strict=False) for v in values)


def _version_key(value: str) -> tuple:
    """The first dotted number in the string, compared numerically and padded:
    7.2.10 > 7.2.9; "v7.2.10,build1706" and "7.2.10" compare equal; 7.2 == 7.2.0.
    Build and train suffixes are not ordered. Empty when there is no number."""
    m = re.search(r"\d+(?:\.\d+)*", str(value))
    if not m:
        return ()
    parts = [int(p) for p in m.group(0).split(".")][:8]
    return tuple(parts + [0] * (8 - len(parts)))


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == []


def _text(value: Any) -> str:
    return str(value)[:MAX_SUBJECT].lower()


def eval_leaf(leaf: dict, facts: dict, fields: dict | None = None) -> bool:
    field, op, want = leaf["field"], leaf["operator"], leaf.get("value")
    catalog = fields if fields is not None else FIELDS
    if field not in catalog:          # a field removed since the tree was saved (custom field deleted)
        return op in NEGATIVE
    ftype = catalog[field][0]
    have = facts.get(field)

    if op == "is_empty":
        return _missing(have)
    if op == "is_not_empty":
        return not _missing(have)
    if ftype == "boolean":
        return bool(have) if op == "is_true" else not bool(have)
    if _missing(have):
        return op in NEGATIVE

    if ftype == "ip":
        try:
            addr = ipaddress.ip_address(str(have))
        except ValueError:
            return op in NEGATIVE
        if op in ("in_subnet", "not_in_subnet"):
            inside = any(addr.version == n.version and addr in n for n in _networks(tuple(want)))
            return inside if op == "in_subnet" else not inside
        if op == "in_range":
            a, b = _ip_range(want)
            return addr.version == a.version and int(a) <= int(addr) <= int(b)
        if op == "equals":
            return str(addr) == want
        if op == "in_list":
            return str(addr) in {str(v) for v in want}
        return False

    if ftype == "number":
        try:
            n = float(have)
        except (TypeError, ValueError):
            return False
        if op == "between":
            return want[0] <= n <= want[1]
        return {"eq": n == want, "ne": n != want, "lt": n < want, "lte": n <= want,
                "gt": n > want, "gte": n >= want}.get(op, False)

    if ftype == "set":
        have_set = {str(v).lower() for v in have}
        want_set = {str(v).lower() for v in want}
        if op == "has_any":
            return bool(have_set & want_set)
        if op == "has_all":
            return want_set <= have_set
        if op == "has_none":
            return not (have_set & want_set)
        return False

    if ftype in ("enum", "reference"):
        h = str(have).lower()
        if op == "is":
            return h == str(want).lower()
        if op == "is_not":
            return h != str(want).lower()
        if op == "is_any_of":
            return h in {str(v).lower() for v in want}
        return False

    if ftype == "oid":
        h = normalize_oid(str(have))
        if op == "equals":
            return h == want
        if op in ("starts_with", "not_starts_with"):
            under = h == want or h.startswith(want + ".")
            return under if op == "starts_with" else not under
        if op == "in_list":
            return h in {normalize_oid(v) for v in want}
        if op == "matches":
            return bool(_regex(want).search(h[:MAX_SUBJECT]))
        return False

    if ftype == "date":
        try:
            h, w = date.fromisoformat(str(have)[:10]), date.fromisoformat(str(want))
        except ValueError:
            return False
        return {"on": h == w, "before": h < w, "after": h > w}.get(op, False)

    if ftype == "version" and op.startswith("version_"):
        hk, wk = _version_key(have), _version_key(want)
        if not hk or not wk:
            return False
        return {"version_lt": hk < wk, "version_lte": hk <= wk,
                "version_gt": hk > wk, "version_gte": hk >= wk}[op]

    # text and the text operators of version
    h = _text(have)
    if op == "matches":
        return bool(_regex(want).search(str(have)[:MAX_SUBJECT]))
    if op == "in_list":
        return h in {str(v).lower() for v in want}
    w = str(want).lower()
    return {
        "equals": h == w, "not_equals": h != w,
        "contains": w in h, "not_contains": w not in h,
        "starts_with": h.startswith(w), "ends_with": h.endswith(w),
    }.get(op, False)


def evaluate(node: dict, facts: dict, trace: list | None = None, fields: dict | None = None) -> bool:
    """Whether ``facts`` satisfy the tree. With ``trace`` every evaluated leaf
    appends ``[field, operator, matched]``; groups short-circuit, so a leaf
    that was not needed is not listed."""
    if "op" in node:
        children = node.get("conditions") or []
        op = node["op"]
        if op == "all":
            return all(evaluate(c, facts, trace, fields) for c in children)
        if op == "any":
            return any(evaluate(c, facts, trace, fields) for c in children)
        return not any(evaluate(c, facts, trace, fields) for c in children)
    ok = eval_leaf(node, facts, fields)
    if trace is not None:
        trace.append([node["field"], node["operator"], ok])
    return ok
