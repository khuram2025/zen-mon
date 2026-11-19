"""Interface roles (migrate-193, docs/alert-rule-overrides.md).

A template lists ordered role rules; the first rule that matches an interface
gives it its role. A rule tests one field: ``if_type`` (comma-separated IF-MIB
types, "135,136") or ``if_name`` / ``if_alias`` / ``if_descr`` (a regular
expression, case-sensitive unless it starts with ``(?i)``). An operator can set
one interface's role by hand (``device_interfaces.role``), which wins.

Roles only select interfaces for rules (``alert_rules.if_role``) and label
them; they never change whether an interface is monitored.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable, Optional

ROLES = ("uplink", "access", "wan", "server", "tunnel", "vlan", "loopback", "management")
ROLE_LABELS = {
    "uplink": "Uplink", "access": "Access", "wan": "WAN", "server": "Server-facing",
    "tunnel": "Tunnel", "vlan": "VLAN", "loopback": "Loopback", "management": "Management",
}
FIELDS = ("if_type", "if_name", "if_alias", "if_descr")
MAX_RULES = 50
MAX_PATTERN = 200
_SUBJECT = 255


class RoleRuleError(ValueError):
    pass


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> Optional[re.Pattern]:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _types(pattern: str) -> set[int]:
    out: set[int] = set()
    for part in str(pattern).split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RoleRuleError(f"if_type takes IF-MIB type numbers separated by commas, not {part!r}")
        out.add(int(part))
    if not out:
        raise RoleRuleError("if_type needs at least one type number")
    return out


def validate(rules: Any) -> Optional[list[dict]]:
    """Normalised rules, or None for "none / as the parent". Raises RoleRuleError."""
    if rules is None:
        return None
    if not isinstance(rules, list):
        raise RoleRuleError("interface_roles must be a list")
    if len(rules) > MAX_RULES:
        raise RoleRuleError(f"at most {MAX_RULES} interface role rules")
    out = []
    for n, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise RoleRuleError(f"role rule {n} must be an object")
        role, field, pattern = r.get("role"), r.get("field"), str(r.get("pattern") or "").strip()
        if role not in ROLES:
            raise RoleRuleError(f"role rule {n}: role must be one of {', '.join(ROLES)}")
        if field not in FIELDS:
            raise RoleRuleError(f"role rule {n}: field must be one of {', '.join(FIELDS)}")
        if not pattern or len(pattern) > MAX_PATTERN:
            raise RoleRuleError(f"role rule {n}: pattern is required (at most {MAX_PATTERN} characters)")
        if field == "if_type":
            _types(pattern)
        elif _compiled(pattern) is None:
            raise RoleRuleError(f"role rule {n}: {pattern!r} is not a valid regular expression")
        out.append({"role": role, "field": field, "pattern": pattern})
    return out


def rule_matches(rule: dict, iface: dict) -> bool:
    field, pattern = rule.get("field"), str(rule.get("pattern") or "")
    if field == "if_type":
        try:
            return iface.get("if_type") is not None and int(iface["if_type"]) in _types(pattern)
        except (RoleRuleError, TypeError, ValueError):
            return False
    rx = _compiled(pattern)
    return bool(rx and rx.search(str(iface.get(field) or "")[:_SUBJECT]))


def template_role(rules: Optional[Iterable[dict]], iface: dict) -> Optional[str]:
    """The role the template's rules give this interface, or None."""
    for rule in rules or []:
        if rule_matches(rule, iface):
            return rule.get("role")
    return None


def effective(rules: Optional[Iterable[dict]], iface: dict) -> tuple[Optional[str], Optional[str]]:
    """(role, source): source is "manual", "template" or None."""
    manual = iface.get("role")
    if manual:
        return manual, "manual"
    role = template_role(rules, iface)
    return (role, "template") if role else (None, None)


def effective_rules(row: Optional[dict], parent: Optional[dict] = None) -> Optional[list[dict]]:
    """A template's role rules: its own, else (customised template) its parent's."""
    if not row:
        return None
    own = row.get("interface_roles")
    if own is not None:
        return own
    return (parent or {}).get("interface_roles")
