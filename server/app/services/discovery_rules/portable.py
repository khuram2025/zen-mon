"""Discovery rules phase 3: JSON export/import and starter rules.

Export writes rules with every reference (device group, template, SNMP
credential, discovery profile, network map, parent device) replaced by its
name, so a file moves between appliances. Import resolves names on the
receiving appliance; a rule with a name that does not resolve is refused and
reported, never guessed. Imported rules always arrive as Draft. No secret is
ever part of a rule, so none is exported.

Starter rules are a pack an administrator adds on purpose: each becomes a
Draft rule (``builtin_key``), nothing is created at upgrade.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .actions import ACTIONS
from .facts import REFERENCE_SOURCES

FORMAT = "zenplus-discovery-rules"
VERSION = 1

# source -> (table, name column, order for ties)
NAME_SOURCES: dict[str, tuple[str, str, str]] = {
    "device_groups": ("device_groups", "name", "name"),
    "device_profiles": ("device_profiles", "name", "version DESC, updated_at DESC"),
    "snmp_credentials": ("snmp_credentials", "name", "name"),
    "discovery_profiles": ("discovery_profiles", "name", "name"),
    "manual_maps": ("manual_maps", "name", "updated_at DESC"),
    "devices": ("devices", "hostname", "hostname"),
    "sensors": ("sensors", "name", "name"),
    "agent_policies": ("agent_policies", "name", "name"),
    "service_check_templates": ("service_check_templates", "name", "name"),
}


def _leaf_refs(node: dict):
    if "op" in node:
        for c in node.get("conditions") or []:
            yield from _leaf_refs(c)
    elif REFERENCE_SOURCES.get(node.get("field")):
        yield node


async def _names(db: AsyncSession, source: str, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    table, col, _ = NAME_SOURCES[source]
    rows = (await db.execute(text(f"SELECT id::text, {col} FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                             {"ids": sorted(ids)})).all()
    return {i: n for i, n in rows}


async def export_rules(db: AsyncSession, rules: list[dict]) -> dict:
    """``rules`` as stored (runner.rule_dict plus description), in order."""
    wanted: dict[str, set[str]] = {}
    for r in rules:
        for leaf in _leaf_refs(r["condition"]):
            vals = leaf["value"] if isinstance(leaf["value"], list) else [leaf["value"]]
            wanted.setdefault(REFERENCE_SOURCES[leaf["field"]], set()).update(str(v) for v in vals)
        for a in r["actions"]:
            spec = ACTIONS.get(a["key"])
            if spec and spec.source:
                wanted.setdefault(spec.source, set()).add(str(a["value"]))
        for p in r.get("profile_ids") or []:
            wanted.setdefault("discovery_profiles", set()).add(str(p))
    names = {src: await _names(db, src, ids) for src, ids in wanted.items()}

    def ref(source, value):
        return {"$ref": source, "name": names.get(source, {}).get(str(value), str(value))}

    out = []
    for r in rules:
        cond = copy.deepcopy(r["condition"])
        for leaf in _leaf_refs(cond):
            src = REFERENCE_SOURCES[leaf["field"]]
            leaf["value"] = [ref(src, v) for v in leaf["value"]] if isinstance(leaf["value"], list) \
                else ref(src, leaf["value"])
        acts = []
        for a in r["actions"]:
            a = dict(a)
            spec = ACTIONS.get(a["key"])
            if spec and spec.source:
                a["value"] = ref(spec.source, a["value"])
            acts.append(a)
        out.append({
            "name": r["name"], "description": r.get("description"), "mode": r["mode"],
            "asset_kinds": r["asset_kinds"], "triggers": r["triggers"],
            "enforcement": r.get("enforcement", "set_once"), "on_unmatch": r.get("on_unmatch", "keep"),
            "stop_processing": r["stop_processing"],
            "max_changes_per_run": r.get("max_changes_per_run"),
            "profiles": [ref("discovery_profiles", p) for p in r.get("profile_ids") or []] or None,
            "condition": cond, "actions": acts,
        })
    return {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
            "rules": out}


class Unresolved(Exception):
    pass


async def _resolve(db: AsyncSession, value: Any, notes: list[str]) -> str:
    if not (isinstance(value, dict) and "$ref" in value):
        return value
    source, name = value.get("$ref"), str(value.get("name") or "")
    if source not in NAME_SOURCES or not name:
        raise Unresolved(f"unknown reference {value!r}")
    table, col, order = NAME_SOURCES[source]
    rows = (await db.execute(text(f"SELECT id::text FROM {table} WHERE {col} = :n ORDER BY {order}"),
                             {"n": name})).all()
    if not rows:
        raise Unresolved(f"no {source.replace('_', ' ')[:-1]} named {name!r} here")
    if len(rows) > 1:
        if source == "device_profiles":
            notes.append(f"{len(rows)} templates are named {name!r}; used the newest version")
        else:
            raise Unresolved(f"{len(rows)} {source.replace('_', ' ')} are named {name!r}; make the name unique")
    return rows[0][0]


async def resolve_rule(db: AsyncSession, rule: dict) -> tuple[dict, list[str]]:
    """The rule with names turned back into ids; ``Unresolved`` names the problem."""
    if not isinstance(rule, dict) or not rule.get("name"):
        raise Unresolved("a rule needs a name")
    notes: list[str] = []
    r = copy.deepcopy(rule)
    for leaf in _leaf_refs(r.get("condition") or {"op": "all", "conditions": []}):
        if isinstance(leaf.get("value"), list):
            leaf["value"] = [await _resolve(db, v, notes) for v in leaf["value"]]
        else:
            leaf["value"] = await _resolve(db, leaf.get("value"), notes)
    for a in r.get("actions") or []:
        a["value"] = await _resolve(db, a.get("value"), notes)
    r["profile_ids"] = [await _resolve(db, p, notes) for p in r.get("profiles") or []] or None
    r.pop("profiles", None)
    return r, notes


# ── starter rules ───────────────────────────────────────────────────────────

def _oid(prefix: str) -> dict:
    return {"op": "all", "conditions": [{"field": "sys_object_id", "operator": "starts_with", "value": prefix}]}


def _vendor(key, name, prefix, tag, device_type=None, desc=""):
    acts = [{"key": "tags", "value": [tag]}, {"key": "decision", "value": "review"}]
    if device_type:
        acts.append({"key": "device_type", "value": device_type})
    return {"key": key, "name": name, "description": desc or f"Scan results under the {name.split(' ')[0]} "
            f"enterprise OID ({prefix}): tag them '{tag}' and keep them for review.",
            "asset_kinds": ["device"], "triggers": ["discovery"], "condition": _oid(prefix), "actions": acts}


STARTERS: list[dict] = [
    _vendor("fortigate", "Fortinet FortiGate firewalls", "1.3.6.1.4.1.12356.101", "fortinet", "firewall"),
    _vendor("fortinet", "Fortinet devices", "1.3.6.1.4.1.12356", "fortinet"),
    _vendor("cisco", "Cisco devices", "1.3.6.1.4.1.9", "cisco"),
    _vendor("f5", "F5 BIG-IP load balancers", "1.3.6.1.4.1.3375", "f5", "load_balancer"),
    _vendor("mikrotik", "MikroTik routers", "1.3.6.1.4.1.14988", "mikrotik", "router"),
    _vendor("aruba", "Aruba wireless controllers", "1.3.6.1.4.1.14823", "aruba", "wireless_controller"),
    _vendor("juniper", "Juniper devices", "1.3.6.1.4.1.2636", "juniper"),
    {"key": "printers", "name": "Ignore printers (example)",
     "description": "Example: scan results with a printing port (9100, 515 or 631) and no SNMP vendor match "
                    "are ignored. Review before activating.",
     "asset_kinds": ["device"], "triggers": ["discovery"],
     "condition": {"op": "all", "conditions": [
         {"field": "open_ports", "operator": "has_any", "value": ["9100", "515", "631"]},
         {"field": "device_type", "operator": "not_equals", "value": "server"}]},
     "actions": [{"key": "decision", "value": "ignore"}]},
    {"key": "ip_phones", "name": "Ignore IP phones (example)",
     "description": "Example: scan results answering SIP (5060) or SCCP (2000) whose name starts with SEP are "
                    "ignored. Review before activating.",
     "asset_kinds": ["device"], "triggers": ["discovery"],
     "condition": {"op": "all", "conditions": [
         {"field": "open_ports", "operator": "has_any", "value": ["5060", "2000"]},
         {"field": "hostname", "operator": "starts_with", "value": "SEP"}]},
     "actions": [{"key": "decision", "value": "ignore"}]},
]
STARTER_BY_KEY = {s["key"]: s for s in STARTERS}
