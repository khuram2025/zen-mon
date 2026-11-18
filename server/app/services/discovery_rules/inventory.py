"""Discovery rules on devices and servers already in inventory (phase 2).

The same ``engine.evaluate`` that classifies scan results decides what the
rules want for an existing asset. ``plan_changes`` (pure) turns that plan into
the changes to make, using the asset's current values and the assignments
rules made earlier (``discovery_engine_assignments``):

* a value with no assignment that differs from the rule's value is changed and
  an assignment records the value it replaced;
* a value a rule set that was edited by hand since is a *manual edit*: a
  ``set_once`` rule (the default) keeps it and reports ``kept_manual_edit``;
  an ``enforce`` rule puts its value back;
* tags are only ever added. A rule-added tag removed by hand is a manual edit
  in the same way;
* the engine never deletes an asset, never turns monitoring off and never
  replaces a device's own (inline) SNMP settings with a credential.

Manual runs go preview first: ``preview_inventory`` returns the changes and a
digest; ``apply_inventory`` recomputes them under the apply lock and refuses
when the digest differs (something changed since the preview), so what is
applied is exactly what was previewed. Undo restores every value that still
equals what the rule set and reports the rest. The "when added" sweep applies
rules with the ``created`` trigger to every new device and server once.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_rules import DiscoveryEngineRun, DiscoveryEngineRunItem

from . import engine
from .facts import facts_from_child, facts_from_device, facts_from_server

logger = logging.getLogger("zenplus.discovery_rules")

APPLY_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('discovery-rules-apply', 0))"
MAX_ASSETS = 20000
STATE_KEY = "discovery_rules_state"

# action key -> column, per asset kind
WRITABLE: dict[str, dict[str, str]] = {
    "device": {
        "group": "group_id", "template": "profile_id", "device_type": "device_type", "location": "location",
        "owner": "owner", "snmp_credential": "snmp_credential_id", "ping_interval": "ping_interval",
        "snmp_interval": "snmp_poll_interval", "ping_enabled": "ping_enabled", "tags": "tags",
    },
    "server": {"owner": "owner", "environment": "environment", "tags": "tags", "map": "map",
               "agent_policy": "agent_policy"},
    "child": {"group": "group_id", "location": "location", "owner": "owner", "tags": "tags", "map": "map"},
}
WRITABLE["device"].update({"map": "map", "dependency_parent": "dependency_parent", "sensor": "sensor",
                           "service_template": "service_template", "interfaces": "interfaces"})
# Set-membership values: the asset is on these maps / depends on these parents
# / is monitored from these sensors / has a check from these service check
# templates. Like tags they are only added; item_key is the map, parent,
# sensor or template id.
SET_KEYS = {"map", "dependency_parent", "sensor", "service_template"}
# Created service checks are never deleted by undo or un-match.
KEEP_ON_UNDO = {"service_template"}
# Per-item single values: interface monitoring, item_key = the interface
# (its ifName in lower case, or "#<ifIndex>" when the name is empty or not unique).
ITEM_KEYS = {"interfaces"}
KIND_TABLE = {"device": "devices", "child": "devices", "server": "servers"}
_UUID_KEYS = {"group", "template", "snmp_credential", "agent_policy"}
_INT_KEYS = {"ping_interval", "snmp_interval"}

DEVICE_SQL = """
SELECT d.id, d.hostname, host(d.ip_address) AS ip_address, d.sys_object_id, d.sys_descr, d.vendor, d.model,
       d.device_type, d.os_version, d.serial_number, d.snmp_credential_id, d.tags, d.group_id, d.profile_id,
       d.location, d.owner, d.status, d.ping_enabled, d.snmp_enabled, d.ping_interval, d.snmp_poll_interval,
       d.created_at
FROM devices d
WHERE d.poll_mode IS DISTINCT FROM 'via_controller'
"""
CHILD_SQL = """
SELECT d.id, d.hostname, host(d.ip_address) AS ip_address, d.vendor, d.model, d.device_type, d.os_version,
       d.serial_number, d.tags, d.group_id, d.location, d.owner, d.status, d.created_at, d.managed_source,
       c.hostname AS controller
FROM devices d LEFT JOIN devices c ON c.id = d.managed_by_device_id
WHERE d.poll_mode = 'via_controller'
"""
SERVER_SQL = """
SELECT s.id, s.display_name, s.hostname, s.fqdn, host(s.primary_ip) AS primary_ip, s.os_name, s.os_version,
       s.os_type, s.collection_mode, s.status, s.environment, s.owner, s.tags, s.created_at, s.site_id,
       si.name AS site_name, ag.policy_id AS agent_policy_id, ag.agents AS agent_count, ag.platforms AS agent_platforms
FROM servers s
LEFT JOIN sites si ON si.id = s.site_id
LEFT JOIN LATERAL (SELECT (array_agg(a.policy_id ORDER BY a.created_at, a.id))[1] AS policy_id, count(*) AS agents,
                          array_agg(DISTINCT a.platform) AS platforms
                   FROM agents a WHERE a.server_id = s.id) ag ON true
WHERE true
"""


# ── values ──────────────────────────────────────────────────────────────────

def norm(key: str, value: Any) -> Any:
    """Comparable form of a setting value."""
    if value is None or value == "":
        return None
    if key in _UUID_KEYS:
        return str(value).lower()
    if key in _INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if key == "ping_enabled":
        return bool(value)
    if key == "device_type":
        return str(value).lower()
    if key == "tags":
        return [str(t) for t in (value or [])]
    return str(value)


def _tags(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    return [str(t) for t in (raw or []) if t is not None and str(t).strip()]


def current_values(kind: str, row: Any) -> dict[str, Any]:
    g = (lambda k: row.get(k)) if isinstance(row, dict) else (lambda k: getattr(row, k, None))
    sets = {"map": [str(x).lower() for x in (g("_maps") or [])],
            "dependency_parent": [str(x).lower() for x in (g("_parents") or [])]}
    if kind == "server":
        return {"owner": g("owner"), "environment": g("environment"), "tags": _tags(g("tags")), **sets,
                "agent_policy": g("agent_policy_id"), "_has_agent": bool(g("agent_count")),
                "_agent_platforms": [p for p in (g("agent_platforms") or []) if p],
                "_policy_platforms": g("_policy_platforms") or {}}
    if kind == "child":
        return {"group": g("group_id"), "location": g("location"), "owner": g("owner"), "tags": _tags(g("tags")),
                **sets}
    rows = iface_rows(g("_ifaces"))
    return {**sets,
        "sensor": [str(x).lower() for x in (g("_sensor_ids") or [])],
        "service_template": [str(x).lower() for x in (g("_svc_templates") or [])],
        "interfaces": {k: bool(r.get("monitored")) for k, r in rows.items()}, "_iface_rows": rows,
        "group": g("group_id"), "template": g("profile_id"), "device_type": g("device_type"),
        "location": g("location"), "owner": g("owner"), "snmp_credential": g("snmp_credential_id"),
        "ping_interval": g("ping_interval"), "snmp_interval": g("snmp_poll_interval"),
        "ping_enabled": g("ping_enabled"), "tags": _tags(g("tags")),
        "_snmp_enabled": bool(g("snmp_enabled")),
    }


def iface_rows(ifaces) -> dict[str, dict]:
    """{item_key: interface row}: the ifName in lower case, or "#<ifIndex>"
    when the name is empty or shared by several interfaces (the poller keeps
    an interface's monitored flag by name the same way)."""
    if not ifaces:
        return {}
    names: dict[str, int] = {}
    for i in ifaces:
        n = str(i.get("if_name") or "").strip().lower()
        if n:
            names[n] = names.get(n, 0) + 1
    out: dict[str, dict] = {}
    for i in ifaces:
        n = str(i.get("if_name") or "").strip().lower()
        out[n if n and names[n] == 1 else f"#{i.get('if_index')}"] = i
    return out


def needs(rules: Iterable[dict]) -> set[str]:
    """Extra loaders the rules need: facts they test and actions they use
    (interfaces, topology, sensors, baselines, service_checks)."""
    from .facts import EXTRA_FACTS
    out: set[str] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        if "op" in node:
            for c in node.get("conditions") or []:
                walk(c)
        elif node.get("field") in EXTRA_FACTS:
            out.add(EXTRA_FACTS[node["field"]])

    for r in rules:
        walk(r.get("condition"))
        for a in r.get("actions") or []:
            if a.get("key") == "interfaces":
                out.add("interfaces")
            elif a.get("key") == "service_template":
                out.add("service_checks")
    return out


ALL_EXTRAS = frozenset({"interfaces", "topology", "sensors", "baselines", "service_checks"})


def facts_for(kind: str, row: Any) -> dict:
    if kind == "server":
        return facts_from_server(row)
    if kind == "child":
        return facts_from_child(row)
    return facts_from_device(row)


# ── the pure planner ────────────────────────────────────────────────────────

def plan_changes(kind: str, asset_id: str, current: dict, plan: dict, owned: dict,
                 rules_by_id: dict[str, dict], *, revert_rules: Iterable[str] = ()) -> list[dict]:
    """Changes the rules' ``plan`` makes to one asset.

    ``owned`` maps (action_key, item_key) to the assignment row ({value, ...}).
    Outcomes: ``apply``; ``kept_manual_edit`` (a set-once rule leaves a hand
    edit alone); ``not_reversible`` (a credential would replace the device's
    own SNMP settings, which a rule never does)."""
    writable = WRITABLE[kind]
    out: list[dict] = []

    def ref(entry):
        rule = rules_by_id.get(entry["rule_id"]) or {}
        return {"rule_id": entry["rule_id"], "rule_name": entry.get("rule_name"),
                "rule_revision": rule.get("revision"), "enforce": rule.get("enforcement") == "enforce"}

    def set_entry(key, entry):
        """Add the asset to a map / give it a parent: only ever added."""
        k = str(entry["value"]).lower()
        if k in {str(x).lower() for x in current.get(key) or []}:
            return
        if key == "dependency_parent" and k == str(asset_id).lower():
            return   # a device never depends on itself
        r = ref(entry)
        base = {"asset_kind": kind, "asset_id": str(asset_id), "action_key": key, "item_key": k,
                "before": None, "after": k, "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                "rule_revision": r["rule_revision"]}
        if owned.get((key, k)) is not None and not r["enforce"]:
            out.append({**base, "outcome": "kept_manual_edit",
                        "detail": "Removed by hand after a rule added it; the rule adds it only once"})
            return
        out.append({**base, "outcome": "apply"})

    for key, entry in (plan.get("settings") or {}).items():
        if key == "decision" or key not in writable:
            continue
        if key in SET_KEYS:
            set_entry(key, entry)
            continue
        if key == "agent_policy" and not current.get("_has_agent"):
            continue   # no agent to run the policy
        r = ref(entry)
        desired, cur = norm(key, entry["value"]), norm(key, current.get(key))
        if key == "agent_policy" and desired != cur:
            want = (current.get("_policy_platforms") or {}).get(desired)
            have = set(current.get("_agent_platforms") or [])
            if want and want != "any" and have and have != {want}:
                out.append({"asset_kind": kind, "asset_id": str(asset_id), "action_key": key, "item_key": "",
                            "before": cur, "after": desired, "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                            "rule_revision": r["rule_revision"], "outcome": "not_ready",
                            "detail": f"The policy is for {want} agents; this server's agent runs "
                                      f"{', '.join(sorted(have))}"})
                continue
        base = {"asset_kind": kind, "asset_id": str(asset_id), "action_key": key, "item_key": "",
                "before": cur, "after": desired, "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                "rule_revision": r["rule_revision"]}
        a = owned.get((key, ""))
        edited = a is not None and norm(key, a.get("value")) != cur
        if edited and not r["enforce"]:
            if cur != desired:
                out.append({**base, "outcome": "kept_manual_edit",
                            "detail": "Changed by hand after a rule set it; the rule sets it only once"})
            continue
        if cur == desired:
            continue
        if key == "snmp_credential" and cur is None and current.get("_snmp_enabled"):
            out.append({**base, "outcome": "not_reversible",
                        "detail": "The device has its own SNMP settings; a rule does not replace them"})
            continue
        out.append({**base, "outcome": "apply",
                    "detail": "Re-applied over a manual edit (enforce)" if edited else None})

    if "tags" in writable:
        have = {t.lower() for t in current.get("tags") or []}
        for t in plan.get("tags") or []:
            k = t["value"].lower()
            if k in have:
                continue
            r = ref(t)
            base = {"asset_kind": kind, "asset_id": str(asset_id), "action_key": "tags", "item_key": k,
                    "before": None, "after": t["value"], "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                    "rule_revision": r["rule_revision"]}
            if owned.get(("tags", k)) is not None and not r["enforce"]:
                out.append({**base, "outcome": "kept_manual_edit",
                            "detail": "Removed by hand after a rule added it; the rule adds it only once"})
                continue
            out.append({**base, "outcome": "apply"})
            have.add(k)
    for key, entries in (plan.get("additive") or {}).items():
        if key in writable and key in SET_KEYS:
            for entry in entries:
                set_entry(key, entry)

    # Interfaces: for each interface the first matching pattern (rules in
    # priority order, patterns in rule order) decides monitored or not.
    patterns = (plan.get("additive") or {}).get("interfaces") or []
    if patterns and "interfaces" in writable:
        from .actions import interface_matches
        for item, iface in (current.get("_iface_rows") or {}).items():
            hit = next((e for e in patterns if interface_matches(e["value"], iface)), None)
            if hit is None:
                continue
            desired = hit["value"]["set"] == "monitor"
            cur = bool(iface.get("monitored"))
            r = ref(hit)
            base = {"asset_kind": kind, "asset_id": str(asset_id), "action_key": "interfaces", "item_key": item,
                    "before": cur, "after": desired, "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                    "rule_revision": r["rule_revision"]}
            a = owned.get(("interfaces", item))
            edited = a is not None and bool(a.get("value")) != cur
            if edited and not r["enforce"]:
                if cur != desired:
                    out.append({**base, "outcome": "kept_manual_edit",
                                "detail": "Changed by hand after a rule set it; the rule sets it only once"})
                continue
            if cur == desired:
                continue
            out.append({**base, "outcome": "apply",
                        "detail": "Re-applied over a manual edit (enforce)" if edited else None})

    # Un-match: values owned by a rule with on_unmatch = revert that no longer
    # matches this asset go back to what they were, if still as the rule set them.
    revert = set(revert_rules)
    if revert:
        touched = {(c["action_key"], c["item_key"]) for c in out}
        for (key, item), a in owned.items():
            if a.get("rule_id") not in revert or (key, item) in touched or key not in writable:
                continue
            base = {"asset_kind": kind, "asset_id": str(asset_id), "action_key": key, "item_key": item,
                    "rule_id": a.get("rule_id"), "rule_name": a.get("rule_name"),
                    "rule_revision": (rules_by_id.get(a.get("rule_id")) or {}).get("revision")}
            if key in KEEP_ON_UNDO:
                continue   # created service checks stay
            if key in ITEM_KEYS:
                cur = (current.get(key) or {}).get(item)
                if cur is None or cur != a.get("value"):
                    continue   # interface gone, or changed by hand since
                prev = a.get("previous")
                if prev is None or bool(prev) == cur:
                    continue
                out.append({**base, "before": cur, "after": bool(prev), "outcome": "revert",
                            "detail": "The asset no longer matches the rule (revert on un-match)"})
                continue
            if key == "tags" or key in SET_KEYS:
                present = item in {str(x).lower() for x in current.get(key) or []}
                if present:
                    out.append({**base, "before": a.get("value"), "after": None, "outcome": "revert",
                                "detail": "The asset no longer matches the rule (revert on un-match)"})
                continue
            cur = norm(key, current.get(key))
            if cur != norm(key, a.get("value")):
                continue   # changed by hand since: left alone, the assignment is dropped on apply
            prev = norm(key, a.get("previous"))
            if key == "snmp_credential" and prev is None:
                continue   # never clears a credential
            out.append({**base, "before": cur, "after": prev, "outcome": "revert",
                        "detail": "The asset no longer matches the rule (revert on un-match)"})
    return out


def digest(changes: Iterable[dict]) -> str:
    keys = ("asset_kind", "asset_id", "action_key", "item_key", "before", "after", "rule_id", "outcome")
    rows = sorted(json.dumps([c.get(k) for k in keys], default=str) for c in changes)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:32]


def summarise(changes: list[dict], assets: int, matched: int) -> dict:
    out: dict[str, Any] = {"assets": assets, "matched": matched, "changes": 0, "kept_manual_edit": 0,
                           "not_reversible": 0, "reverts": 0, "assets_changed": 0, "by_action": {}}
    changed = set()
    for c in changes:
        if c["outcome"] == "revert":
            out["reverts"] += 1
            changed.add((c["asset_kind"], c["asset_id"]))
        elif c["outcome"] == "apply":
            out["changes"] += 1
            changed.add((c["asset_kind"], c["asset_id"]))
            out["by_action"][c["action_key"]] = out["by_action"].get(c["action_key"], 0) + 1
        else:
            out[c["outcome"]] = out.get(c["outcome"], 0) + 1
    out["assets_changed"] = len(changed)
    return out


# ── loading ─────────────────────────────────────────────────────────────────

def _filters(kind: str, targets: dict, params: dict) -> str:
    dev = kind in ("device", "child")
    alias = "d" if dev else "s"
    ip_col = "d.ip_address" if dev else "s.primary_ip"
    name_cols = ("d.hostname",) if dev else ("s.display_name", "s.hostname")
    sql = []
    ids = [str(i) for i in (targets.get(f"{kind}_ids") or targets.get("ids") or [])]
    if ids:
        sql.append(f"{alias}.id = ANY(CAST(:ids AS uuid[]))")
        params["ids"] = ids
    if dev and targets.get("group_ids"):
        sql.append("d.group_id = ANY(CAST(:groups AS uuid[]))")
        params["groups"] = [str(g) for g in targets["group_ids"]]
    if targets.get("tags"):
        sql.append(f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({alias}.tags, '[]'::jsonb)) _t"
                   " WHERE lower(_t) = ANY(CAST(:tags AS text[])))")
        params["tags"] = [str(t).lower() for t in targets["tags"]]
    if targets.get("subnet"):
        sql.append(f"{ip_col} <<= CAST(:subnet AS cidr)")
        params["subnet"] = str(ipaddress.ip_network(str(targets["subnet"]), strict=False))
    if targets.get("query"):
        q = " OR ".join(f"{c} ILIKE :q" for c in name_cols) + f" OR {ip_col}::text ILIKE :q"
        sql.append(f"({q})")
        params["q"] = f"%{targets['query']}%"
    if targets.get("scope") is not None:
        sql.append(f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({alias}.tags, '[]'::jsonb)) _v"
                   " WHERE lower(_v) = ANY(CAST(:vis AS text[])))")
        params["vis"] = list(targets["scope"])
    return "".join(f" AND {s}" for s in sql)


async def load_assets(db: AsyncSession, targets: dict, *, limit: int = MAX_ASSETS,
                      extras: Iterable[str] = ()) -> list[tuple[str, dict]]:
    """[(kind, row)] for the manual-run target filter.

    ``targets``: kinds (device, server, child), ids / device_ids /
    server_ids / child_ids, group_ids, tags, subnet, query, and ``scope``
    (visible tags) when the caller's view is limited. Controller-managed
    children load only as kind ``child``."""
    out: list[tuple[str, dict]] = []
    kinds = targets.get("kinds") or ["device", "server"]
    for kind in ("device", "child", "server"):
        if kind not in kinds or len(out) >= limit:
            continue
        params: dict = {"lim": limit - len(out)}
        base = {"device": DEVICE_SQL, "child": CHILD_SQL, "server": SERVER_SQL}[kind]
        order = "s.display_name" if kind == "server" else "d.hostname"
        rows = (await db.execute(text(base + _filters(kind, targets, params) + f" ORDER BY {order} LIMIT :lim"),
                                 params)).mappings().all()
        out.extend((kind, dict(r)) for r in rows)
    await attach_sets(db, out, extras)
    return out


async def attach_sets(db: AsyncSession, assets: list[tuple[str, dict]], extras: Iterable[str] = ()) -> None:
    """Current map placements (``_maps``), dependency parents (``_parents``),
    sensor assignments (``_sensor_ids``) and service checks made from templates
    (``_svc_templates``); with ``extras`` also interfaces, topology, sensors and
    sites, software baselines (see ``needs``)."""
    extras = set(extras)
    dev_ids = [str(r["id"]) for k, r in assets if k in ("device", "child")]
    srv_ids = [str(r["id"]) for k, r in assets if k == "server"]
    maps: dict = {}
    parents: dict = {}
    if dev_ids:
        for aid, mid in (await db.execute(text(
                "SELECT device_id::text, map_id::text FROM manual_map_nodes WHERE device_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": dev_ids})).all():
            maps.setdefault(aid, []).append(mid)
        for aid, pid in (await db.execute(text(
                "SELECT child_device_id::text, parent_device_id::text FROM topology_dependencies"
                " WHERE child_device_id = ANY(CAST(:ids AS uuid[]))"), {"ids": dev_ids})).all():
            parents.setdefault(aid, []).append(pid)
    if srv_ids:
        for aid, mid in (await db.execute(text(
                "SELECT server_id::text, map_id::text FROM manual_map_nodes WHERE server_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": srv_ids})).all():
            maps.setdefault(aid, []).append(mid)
    for _, r in assets:
        r["_maps"] = maps.get(str(r["id"]), [])
        r["_parents"] = parents.get(str(r["id"]), [])
    devices = [r for k, r in assets if k == "device"]
    if devices:
        await _attach_device_extras(db, devices, extras)
    servers = [r for k, r in assets if k == "server"]
    if servers:
        platforms = {str(i).lower(): p for i, p in (await db.execute(text(
            "SELECT id::text, platform FROM agent_policies"))).all()}
        for r in servers:
            r["_policy_platforms"] = platforms
    if servers and "baselines" in extras:
        await _attach_baselines(db, servers)


def check_name(template_name: str, hostname: str | None) -> str:
    """The name ``service_check_service.apply_template`` gives a check."""
    prefix = (template_name or "").strip()
    host = hostname or ""
    return f"{prefix} · {host}" if prefix else host


async def _attach_device_extras(db: AsyncSession, devices: list[dict], extras: set[str]) -> None:
    ids = [str(r["id"]) for r in devices]
    by_id = {str(r["id"]): r for r in devices}
    sensors: dict = {}
    for aid, sid in (await db.execute(text(
            "SELECT target_id::text, sensor_id::text FROM sensor_assignments"
            " WHERE target_type = 'device' AND target_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})).all():
        sensors.setdefault(aid, []).append(sid)
    templates = (await db.execute(text("SELECT id::text, name FROM service_check_templates"))).all()
    names: dict = {}
    if templates:
        for aid, name in (await db.execute(text(
                "SELECT device_id::text, name FROM service_checks WHERE device_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids})).all():
            names.setdefault(aid, set()).add(name)
    for aid, r in by_id.items():
        r["_sensor_ids"] = sensors.get(aid, [])
        have = names.get(aid, set())
        r["_svc_templates"] = [tid for tid, tname in templates if check_name(tname, r.get("hostname")) in have]
    if "interfaces" in extras:
        ifaces: dict = {}
        for row in (await db.execute(text(
                "SELECT device_id::text AS device_id, if_index, if_name, if_alias, if_descr, if_type, if_speed,"
                " configured_speed_bps, monitored FROM device_interfaces"
                " WHERE device_id = ANY(CAST(:ids AS uuid[])) ORDER BY if_index"), {"ids": ids})).mappings().all():
            ifaces.setdefault(row["device_id"], []).append(dict(row))
        for aid, r in by_id.items():
            r["_ifaces"] = ifaces.get(aid, [])
    if "topology" in extras:
        neighbors = await current_neighbors(db, ids)
        pnames: dict = {}
        for aid, host in (await db.execute(text(
                "SELECT td.child_device_id::text, p.hostname FROM topology_dependencies td"
                " JOIN devices p ON p.id = td.parent_device_id WHERE td.child_device_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids})).all():
            if host:
                pnames.setdefault(aid, []).append(host)
        for aid, r in by_id.items():
            r["_neighbors"] = sorted(neighbors.get(aid, set()), key=str.lower)
            r["_parent_names"] = pnames.get(aid, [])
    if "sensors" in extras:
        vant: dict = {}
        for aid, poller, sname, site in (await db.execute(text(
                "SELECT v.device_id::text, v.poller_id, s.name, si.name FROM device_monitoring_vantages v"
                " LEFT JOIN sensors s ON s.id = v.sensor_id LEFT JOIN sites si ON si.id = s.site_id"
                " WHERE v.device_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})).all():
            vant.setdefault(aid, []).append((poller, sname, site))
        for aid, r in by_id.items():
            rows = vant.get(aid, [])
            r["_sensors"] = sorted({n for p, n, _ in rows if p != "central" and n})
            r["_sites"] = sorted({s for p, _, s in rows if p != "central" and s})
            r["_central"] = any(p == "central" for p, _, _ in rows)


async def current_neighbors(db: AsyncSession, device_ids: list[str]) -> dict[str, set[str]]:
    """{device id: hostnames of monitored LLDP/CDP neighbours with current
    evidence}: the network maps' rule (``map_topology``): seen within 24 h,
    both ports present and up. Stale adjacencies are not neighbours."""
    from app.api.v1.manual_maps import _load_topology
    from app.services import map_topology as topo
    if not device_ids:
        return {}
    edges, ifaces, hostnames = await _load_topology(db, device_ids)
    out: dict[str, set[str]] = {}
    wanted = set(device_ids)
    for pair, members in topo.build_adjacencies(edges, ifaces, hostnames).items():
        if not any(not m.stale for m in members):
            continue
        a, b = tuple(pair)
        for me, other in ((a, b), (b, a)):
            if me in wanted and hostnames.get(other):
                out.setdefault(me, set()).add(hostnames[other])
    return out


async def _attach_baselines(db: AsyncSession, servers: list[dict]) -> None:
    from app.services.baseline_service import baseline_in_scope
    baselines = [dict(b) for b in (await db.execute(text(
        "SELECT id, name, enabled, os_type, site_id, match_tags FROM software_baselines WHERE enabled = TRUE"
        " ORDER BY name"))).mappings().all()]
    ids = [str(r["id"]) for r in servers]
    results: dict = {}
    for sid, bad, total in (await db.execute(text(
            "SELECT server_id::text, count(*) FILTER (WHERE status <> 'compliant'), count(*)"
            " FROM server_baseline_results WHERE server_id = ANY(CAST(:ids AS uuid[])) GROUP BY server_id"),
            {"ids": ids})).all():
        results[sid] = (bad, total)
    for r in servers:
        r["_baselines"] = [b["name"] for b in baselines if baseline_in_scope(b, r)]
        bad, total = results.get(str(r["id"]), (0, 0))
        r["_baseline_status"] = "not_evaluated" if not total else ("non_compliant" if bad else "compliant")


async def load_owned(db: AsyncSession, assets: list[tuple[str, dict]]) -> dict:
    """{(kind, id): {(action_key, item_key): row}}"""
    out: dict = {}
    for kind in ("device", "child", "server"):
        ids = [str(r["id"]) for k, r in assets if k == kind]
        if not ids:
            continue
        rows = (await db.execute(text(
            "SELECT asset_id::text AS asset_id, action_key, item_key, value, previous, rule_id::text AS rule_id,"
            " rule_name, run_id::text AS run_id, applied_at FROM discovery_engine_assignments"
            " WHERE asset_kind = :k AND asset_id = ANY(CAST(:ids AS uuid[]))"), {"k": kind, "ids": ids})
        ).mappings().all()
        for r in rows:
            out.setdefault((kind, r["asset_id"]), {})[(r["action_key"], r["item_key"])] = dict(r)
    return out


def evaluate_assets(assets: list[tuple[str, dict]], rules: list[dict], owned: dict, *,
                    trigger: str) -> tuple[list[dict], int, dict]:
    """(changes, matched asset count, {(kind, id): plan}) - pure.

    Values owned by an evaluated rule whose ``on_unmatch`` is ``revert`` and
    that no longer matches the asset are reverted (see ``plan_changes``)."""
    rules_by_id = {r["id"]: r for r in rules}
    reverting = {r["id"] for r in rules if r.get("on_unmatch") == "revert"}
    changes: list[dict] = []
    matched = 0
    plans: dict = {}
    for kind, row in assets:
        plan = engine.evaluate(facts_for(kind, row), rules, trigger=trigger, kind=kind)
        aid = str(row["id"])
        plans[(kind, aid)] = plan
        own = owned.get((kind, aid), {})
        revert = reverting - {m["rule_id"] for m in plan["matched"]}
        revert = {r for r in revert if any(a.get("rule_id") == r for a in own.values())}
        if plan["matched"]:
            matched += 1
        if plan["matched"] or revert:
            changes.extend(plan_changes(kind, aid, current_values(kind, row), plan, own, rules_by_id,
                                        revert_rules=revert))
    return changes, matched, plans


def _manual(rules: list[dict]) -> list[dict]:
    """Rules as evaluated by a manual run: their own triggers do not limit it."""
    return [{**r, "triggers": ["manual"]} for r in rules]


async def preview_inventory(db: AsyncSession, rules: list[dict], targets: dict, *, limit: int = MAX_ASSETS) -> dict:
    assets = await load_assets(db, targets, limit=limit, extras=needs(rules))
    owned = await load_owned(db, assets)
    changes, matched, _ = evaluate_assets(assets, _manual(rules), owned, trigger="manual")
    names = {(k, str(r["id"])): (r.get("hostname") or r.get("display_name"),
                                 r.get("ip_address") or r.get("primary_ip")) for k, r in assets}
    for c in changes:
        c["asset_name"], c["ip_address"] = names.get((c["asset_kind"], c["asset_id"]), (None, None))
    return {"changes": changes, "summary": summarise(changes, len(assets), matched), "digest": digest(changes),
            "truncated": len(assets) >= limit}


# ── writing ─────────────────────────────────────────────────────────────────

async def _map_member(db: AsyncSession, kind: str, asset_id: str, map_id: str, add: bool) -> None:
    """Place the asset on a network map (bottom row, to be arranged by hand) or
    take the node off again, pruning annotation cables like the map editor."""
    from app.api.v1 import manual_maps as mm
    field = "server_id" if kind == "server" else "device_id"
    try:
        md = await mm._lock_map(db, map_id)
    except Exception:
        return   # the map was deleted
    if add:
        exists = (await db.execute(text(f"SELECT 1 FROM manual_map_nodes WHERE map_id = :m AND {field} = :a"),
                                   {"m": map_id, "a": asset_id})).first()
        if exists:
            return
        n = (await db.execute(text("SELECT count(*) FROM manual_map_nodes WHERE map_id = :m"), {"m": map_id})).scalar()
        await mm._insert_node(db, map_id, field=field, entity_id=asset_id, x_pct=5 + (n * 6) % 90,
                              y_pct=92 - 6 * ((n * 6) // 90 % 4), metadata={"placed_by": "discovery_rule"})
        await mm._touch(db, map_id)
        return
    ids = [str(r[0]) for r in (await db.execute(text(
        f"DELETE FROM manual_map_nodes WHERE map_id = :m AND {field} = :a RETURNING id"),
        {"m": map_id, "a": asset_id})).all()]
    md, dropped = mm._prune_ann(md, ids)
    if dropped:
        await mm._save_map_metadata(db, map_id, md)
    else:
        await mm._touch(db, map_id)


async def _write(db: AsyncSession, change: dict, value: Any, *, apply_credential=None) -> None:
    """Set one value on the asset (``value`` None clears a single value or
    removes a tag / map placement / dependency)."""
    kind, key = change["asset_kind"], change["action_key"]
    table = KIND_TABLE[kind]
    col = WRITABLE[kind][key]
    params = {"id": change["asset_id"]}
    if key == "map":
        await _map_member(db, kind, change["asset_id"], change["item_key"], value is not None)
        return
    if key == "sensor":
        if value is None:
            await db.execute(text(
                "DELETE FROM sensor_assignments WHERE sensor_id = CAST(:s AS uuid) AND target_type = 'device'"
                " AND target_id = CAST(:id AS uuid)"), {**params, "s": change["item_key"]})
        else:
            await db.execute(text(
                "INSERT INTO sensor_assignments (sensor_id, target_type, target_id, priority)"
                " VALUES (CAST(:s AS uuid), 'device', CAST(:id AS uuid), 100) ON CONFLICT DO NOTHING"),
                {**params, "s": change["item_key"]})
        return
    if key == "service_template":
        if value is None:
            return   # a created service check is never deleted by a rule
        from app.schemas.service_check import ServiceCheckTemplateApply
        from app.services import service_check_service
        try:
            res = await service_check_service.apply_template(
                db, uuid.UUID(change["item_key"]),
                ServiceCheckTemplateApply(device_ids=[uuid.UUID(change["asset_id"])], enabled=True),
                None, commit=False)
        except ValueError as e:   # the template was deleted
            raise NotWritten(str(e)) from None
        if not res.created_ids:
            reason = (res.skipped[0].get("reason") if res.skipped else None) or "not created"
            raise NotWritten(f"No service check created: {reason}")
        return
    if key == "agent_policy":
        await db.execute(text("UPDATE agents SET policy_id = CAST(:v AS uuid), updated_at = now()"
                              " WHERE server_id = CAST(:id AS uuid)"), {**params, "v": value})
        return
    if key == "interfaces":
        item = change["item_key"]
        where = "if_index = CAST(:ix AS integer)" if item.startswith("#") else "lower(if_name) = :ix"
        await db.execute(text(f"UPDATE device_interfaces SET monitored = :v WHERE device_id = CAST(:id AS uuid)"
                              f" AND {where}"),
                         {**params, "v": bool(value), "ix": item[1:] if item.startswith("#") else item})
        return
    if key == "dependency_parent":
        if value is None:
            await db.execute(text(
                "DELETE FROM topology_dependencies WHERE child_device_id = CAST(:id AS uuid)"
                " AND parent_device_id = CAST(:p AS uuid) AND dependency_type = 'uplink'"),
                {**params, "p": change["item_key"]})
        else:
            await db.execute(text(
                "INSERT INTO topology_dependencies (parent_device_id, child_device_id, dependency_type,"
                " suppress_alerts, notes) VALUES (CAST(:p AS uuid), CAST(:id AS uuid), 'uplink', true, :n)"
                " ON CONFLICT DO NOTHING"),
                {**params, "p": change["item_key"], "n": f"Discovery rule: {change.get('rule_name') or ''}"[:500]})
        return
    if key == "tags":
        if value is None:   # remove the tag (undo)
            await db.execute(text(
                f"UPDATE {table} SET tags = COALESCE((SELECT jsonb_agg(t) FROM jsonb_array_elements(tags) t"
                f" WHERE lower(t #>> '{{}}') <> :k), '[]'::jsonb) WHERE id = CAST(:id AS uuid)"),
                {**params, "k": change["item_key"]})
        else:
            await db.execute(text(
                f"UPDATE {table} SET tags = COALESCE(tags, '[]'::jsonb) || jsonb_build_array(CAST(:v AS text))"
                f" WHERE id = CAST(:id AS uuid)"), {**params, "v": value})
        return
    if key == "snmp_credential":
        from app.models.device import Device
        from app.services.device_service import apply_credential as _apply
        device = await db.get(Device, uuid.UUID(change["asset_id"]))
        if device is not None:
            device.snmp_credential_id = uuid.UUID(value) if value else None
            if value:
                await (apply_credential or _apply)(db, device, uuid.UUID(value))
            await db.flush()
        return
    extra = ""
    if key == "template":
        extra = ", profile_source = CASE WHEN :v IS NULL THEN NULL ELSE 'rule' END, profile_assigned_at = now()"
    elif key == "device_type":
        extra = ", device_type_source = CASE WHEN :v IS NULL THEN NULL ELSE 'rule' END"
    if key in _UUID_KEYS:
        expr = "CAST(:v AS uuid)"
    elif key in _INT_KEYS:
        expr = "CAST(:v AS integer)"
    elif key == "ping_enabled":
        expr = "CAST(:v AS boolean)"
    else:
        expr = "CAST(:v AS text)"
    if key == "device_type" and value is None:
        value = "other"   # devices.device_type is not nullable
    await db.execute(text(f"UPDATE {table} SET {col} = {expr}{extra} WHERE id = CAST(:id AS uuid)"),
                     {**params, "v": value})


class NotWritten(Exception):
    """A value that could not be written; recorded as an ``error`` item."""


async def _assign(db: AsyncSession, change: dict, run_id) -> None:
    await db.execute(text(
        "INSERT INTO discovery_engine_assignments (asset_kind, asset_id, action_key, item_key, value, previous,"
        " rule_id, rule_name, rule_revision, run_id, applied_at)"
        " VALUES (:k, CAST(:a AS uuid), :key, :item, CAST(:v AS jsonb), CAST(:p AS jsonb), CAST(:r AS uuid),"
        " :rn, :rv, :run, now())"
        " ON CONFLICT (asset_kind, asset_id, action_key, item_key) DO UPDATE SET value = EXCLUDED.value,"
        " rule_id = EXCLUDED.rule_id, rule_name = EXCLUDED.rule_name, rule_revision = EXCLUDED.rule_revision,"
        " run_id = EXCLUDED.run_id, applied_at = now()"),
        {"k": change["asset_kind"], "a": change["asset_id"], "key": change["action_key"],
         "item": change["item_key"], "v": json.dumps(change["after"]), "p": json.dumps(change["before"]),
         "r": change["rule_id"], "rn": change.get("rule_name"), "rv": change.get("rule_revision"), "run": run_id})


async def _reload(db: AsyncSession, kind: str, asset_id: str, extras: Iterable[str] = ()) -> dict | None:
    if kind == "server":
        sql = SERVER_SQL + " AND s.id = CAST(:id AS uuid) FOR UPDATE OF s"
    else:
        sql = (DEVICE_SQL if kind == "device" else CHILD_SQL) + " AND d.id = CAST(:id AS uuid) FOR UPDATE OF d"
    row = (await db.execute(text(sql), {"id": asset_id})).mappings().first()
    if not row:
        return None
    out = dict(row)
    await attach_sets(db, [(kind, out)], extras)
    return out


def _present(current: dict, key: str, item: str) -> bool:
    return str(item).lower() in {str(x).lower() for x in current.get(key) or []}


async def _drop_assignment(db: AsyncSession, change: dict) -> None:
    await db.execute(text(
        "DELETE FROM discovery_engine_assignments WHERE asset_kind = :k AND asset_id = CAST(:a AS uuid)"
        " AND action_key = :key AND item_key = :item"),
        {"k": change["asset_kind"], "a": change["asset_id"], "key": change["action_key"],
         "item": change.get("item_key") or ""})


def _item(run_id, change: dict, outcome: str, detail: str | None = None) -> DiscoveryEngineRunItem:
    return DiscoveryEngineRunItem(
        run_id=run_id, asset_kind=change["asset_kind"], asset_id=uuid.UUID(change["asset_id"]),
        ip_address=change.get("ip_address"), rule_id=uuid.UUID(change["rule_id"]) if change.get("rule_id") else None,
        rule_name=change.get("rule_name"), rule_revision=change.get("rule_revision"),
        action_key=change["action_key"], item_key=change.get("item_key") or None,
        before=change.get("before"), after=change.get("after"), outcome=outcome,
        detail=detail if detail is not None else change.get("detail"))


async def apply_changes(db: AsyncSession, changes: list[dict], run: DiscoveryEngineRun, *,
                        apply_credential=None) -> dict:
    """Write ``apply`` changes (re-checked against the locked current value),
    record every change as a run item. Caller holds APPLY_LOCK and commits."""
    counts = {"applied": 0, "kept_manual_edit": 0, "not_reversible": 0, "skipped_changed": 0, "reverted": 0,
              "not_ready": 0}
    by_asset: dict = {}
    for c in changes:
        by_asset.setdefault((c["asset_kind"], c["asset_id"]), []).append(c)
    for (kind, aid), group in by_asset.items():
        keys = {c["action_key"] for c in group}
        ready: dict = {}
        if "interfaces" in keys:
            # The poller rewrites a device's interfaces under this lock; take
            # it before the device row, in the poller's order.
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                             {"k": f"interfaces:{aid}"})
        if "sensor" in keys:
            # Result ingestion locks the sensor before the device: same order here.
            sids = sorted({c["item_key"] for c in group if c["action_key"] == "sensor"})
            for r in (await db.execute(text(
                    "SELECT id::text AS id, status, api_key_hash IS NOT NULL AS enrolled, bootstrap_config"
                    " FROM sensors WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id FOR SHARE"),
                    {"ids": sids})).mappings().all():
                pending = bool((r["bootstrap_config"] or {}).get("authorization_pending"))
                ready[r["id"]] = r["enrolled"] and not pending and r["status"] in ("online", "degraded")
        row = await _reload(db, kind, aid, {"interfaces"} if "interfaces" in keys else ())
        if row is None:
            for c in group:
                if c["outcome"] == "apply":
                    db.add(_item(run.id, c, "skipped_changed", "The asset was deleted"))
                    counts["skipped_changed"] += 1
            continue
        current = current_values(kind, row)
        for c in group:
            c.setdefault("ip_address", row.get("ip_address") or row.get("primary_ip"))
            key = c["action_key"]
            setlike = key == "tags" or key in SET_KEYS
            if c["outcome"] == "revert":
                if key in ITEM_KEYS:
                    still = (current.get(key) or {}).get(c["item_key"]) == c["before"]
                else:
                    still = _present(current, key, c["item_key"]) if setlike else \
                        norm(key, current.get(key)) == c["before"]
                if not still:
                    db.add(_item(run.id, c, "skipped_changed", "Changed since the plan was made"))
                    counts["skipped_changed"] += 1
                    continue
                await _write(db, c, None if setlike else c["after"], apply_credential=apply_credential)
                await _drop_assignment(db, c)
                db.add(_item(run.id, c, "reverted"))
                counts["reverted"] += 1
                continue
            if c["outcome"] != "apply":
                db.add(_item(run.id, c, c["outcome"]))
                counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1
                continue
            if key in ITEM_KEYS:
                still = (current.get(key) or {}).get(c["item_key"]) == c["before"]
            elif setlike:
                still = not _present(current, key, c["item_key"])
            else:
                still = norm(key, current.get(key)) == c["before"]
            if key == "agent_policy" and not current.get("_has_agent"):
                still = False
            if not still:
                db.add(_item(run.id, c, "skipped_changed", "Changed since the plan was made"))
                counts["skipped_changed"] += 1
                continue
            if key == "sensor" and not ready.get(c["item_key"]):
                db.add(_item(run.id, c, "not_ready",
                             "The sensor is not online and authorised; it is added once it is"))
                counts["not_ready"] += 1
                continue
            try:
                if key == "service_template":   # creates rows: undo them alone if refused
                    async with db.begin_nested():
                        await _write(db, c, c["after"], apply_credential=apply_credential)
                else:
                    await _write(db, c, c["after"], apply_credential=apply_credential)
            except NotWritten as e:
                db.add(_item(run.id, c, "error", str(e)[:500]))
                counts["error"] = counts.get("error", 0) + 1
                continue
            await _assign(db, c, run.id)
            db.add(_item(run.id, c, "applied"))
            counts["applied"] += 1
    return counts


async def apply_inventory(db: AsyncSession, rules: list[dict], targets: dict, *, expected_digest: str,
                          actor_id, rules_revision: str | None = None) -> dict:
    """Recompute the preview under the apply lock; refuse when it changed.
    Commits. Returns the run summary, or raises ``PreviewStale``."""
    await db.execute(text(APPLY_LOCK))
    pre = await preview_inventory(db, rules, targets)
    if pre["digest"] != expected_digest:
        raise PreviewStale(pre)
    run = DiscoveryEngineRun(trigger="manual", mode="apply", targets=_jsonable(targets),
                             preview_digest=expected_digest, rules_revision=rules_revision or engine.rules_revision(rules),
                             started_by=actor_id, status="running")
    db.add(run)
    await db.flush()
    counts = await apply_changes(db, pre["changes"], run)
    run.counts = {**pre["summary"], **counts}
    run.status = "partial" if counts["skipped_changed"] else "completed"
    run.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return {"engine_run_id": str(run.id), "status": run.status, **run.counts}


class PreviewStale(Exception):
    def __init__(self, preview: dict):
        super().__init__("The inventory or the rules changed since the preview")
        self.preview = preview


def _jsonable(v):
    return json.loads(json.dumps(v, default=str))


# ── undo ────────────────────────────────────────────────────────────────────

class UndoRefused(Exception):
    pass


async def undo_run(db: AsyncSession, run_id: uuid.UUID, *, actor_id, apply_credential=None) -> dict:
    """Restore every value the run set that still equals what it set; report
    the rest. Devices a discovery run imported are listed, never deleted.
    Commits."""
    await db.execute(text(APPLY_LOCK))
    run = await db.get(DiscoveryEngineRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("Run not found")
    if run.mode != "apply" or run.status not in ("completed", "partial") or run.trigger == "undo":
        raise UndoRefused("Only a completed run that changed something can be undone, once")
    imported = [dict(r) for r in (await db.execute(text(
        "SELECT i.asset_id::text AS device_id, host(i.ip_address) AS ip_address, d.hostname"
        " FROM discovery_engine_run_items i JOIN devices d ON d.id = i.asset_id"
        " WHERE i.run_id = :r AND i.action_key = 'decision' AND i.outcome = 'applied'"
        " AND i.after->>'result' = 'imported'"), {"r": run_id})).mappings().all()]
    items = (await db.execute(text(
        "SELECT * FROM discovery_engine_run_items WHERE run_id = :r AND outcome = 'applied'"
        " AND asset_id IS NOT NULL AND asset_kind IN ('device', 'server', 'child') AND action_key <> 'decision'"
        " ORDER BY id DESC"), {"r": run_id})).mappings().all()
    undo = DiscoveryEngineRun(trigger="undo", mode="apply", undo_of=run.id, started_by=actor_id, status="running",
                              discovery_run_id=run.discovery_run_id)
    db.add(undo)
    await db.flush()
    counts = {"reverted": 0, "skipped_changed": 0, "not_reversible": 0}
    # A scan's run (or a re-evaluation of a scan) created devices: their
    # settings came with the import and are not reverted; the devices are
    # listed for a deliberate delete. Inventory runs are reverted value by value.
    for it in ([] if run.discovery_run_id else items):
        c = {"asset_kind": it["asset_kind"], "asset_id": str(it["asset_id"]), "action_key": it["action_key"],
             "item_key": it["item_key"] or "",
             "before": it["after"], "after": it["before"], "rule_id": str(it["rule_id"]) if it["rule_id"] else None,
             "rule_name": it["rule_name"], "rule_revision": it["rule_revision"],
             "ip_address": str(it["ip_address"]) if it["ip_address"] else None}
        key = c["action_key"]
        if key in KEEP_ON_UNDO:
            db.add(_item(undo.id, c, "not_reversible",
                         "The service check the rule created stays; delete it under Service Checks if unwanted"))
            counts["not_reversible"] += 1
            continue
        if key == "interfaces":
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                             {"k": f"interfaces:{c['asset_id']}"})
        row = await _reload(db, c["asset_kind"], c["asset_id"], {"interfaces"} if key == "interfaces" else ())
        if row is None:
            db.add(_item(undo.id, c, "skipped_changed", "The asset was deleted"))
            counts["skipped_changed"] += 1
            continue
        current = current_values(c["asset_kind"], row)
        setlike = key == "tags" or key in SET_KEYS
        if key in ITEM_KEYS:
            still = (current.get(key) or {}).get(c["item_key"]) == it["after"]
        elif setlike:
            still = _present(current, key, c["item_key"])
        else:
            still = norm(key, current.get(key)) == norm(key, it["after"])
        if not still:
            db.add(_item(undo.id, c, "skipped_changed", "Changed since the rule set it; left as it is"))
            counts["skipped_changed"] += 1
            continue
        if key == "snmp_credential" and it["before"] is None:
            db.add(_item(undo.id, c, "not_reversible", "There was no credential before; left in place"))
            counts["not_reversible"] += 1
            continue
        await _write(db, {**c, "action_key": key}, None if setlike else it["before"],
                     apply_credential=apply_credential)
        a = (await db.execute(text(
            "SELECT previous FROM discovery_engine_assignments WHERE asset_kind = :k AND asset_id = CAST(:a AS uuid)"
            " AND action_key = :key AND item_key = :item"),
            {"k": c["asset_kind"], "a": c["asset_id"], "key": key, "item": c["item_key"]})).first()
        if a is not None and (setlike or key in ITEM_KEYS or norm(key, a[0]) == norm(key, it["before"])):
            await db.execute(text(
                "DELETE FROM discovery_engine_assignments WHERE asset_kind = :k AND asset_id = CAST(:a AS uuid)"
                " AND action_key = :key AND item_key = :item"),
                {"k": c["asset_kind"], "a": c["asset_id"], "key": key, "item": c["item_key"]})
        elif a is not None:
            await db.execute(text(
                "UPDATE discovery_engine_assignments SET value = CAST(:v AS jsonb), applied_at = now()"
                " WHERE asset_kind = :k AND asset_id = CAST(:a AS uuid) AND action_key = :key AND item_key = :item"),
                {"k": c["asset_kind"], "a": c["asset_id"], "key": key, "item": c["item_key"],
                 "v": json.dumps(it["before"])})
        db.add(_item(undo.id, c, "reverted"))
        counts["reverted"] += 1
    now = datetime.now(timezone.utc)
    undo.counts = {**counts, "imported_devices": len(imported)}
    undo.status = "partial" if counts["skipped_changed"] or counts["not_reversible"] else "completed"
    undo.finished_at = now
    run.status, run.undone_by, run.undone_at = "undone", actor_id, now
    await db.commit()
    return {"engine_run_id": str(undo.id), "undo_of": str(run.id), "status": undo.status, **counts,
            "imported_devices": imported}


# ── explain and conflicts ───────────────────────────────────────────────────

async def explain(db: AsyncSession, kind: str, asset_id: uuid.UUID, rules: list[dict]) -> dict | None:
    params = {"id": str(asset_id)}
    sql = {"device": DEVICE_SQL + " AND d.id = CAST(:id AS uuid)",
           "child": CHILD_SQL + " AND d.id = CAST(:id AS uuid)",
           "server": SERVER_SQL + " AND s.id = CAST(:id AS uuid)"}[kind]
    row = (await db.execute(text(sql), params)).mappings().first()
    if row is None:
        return None
    row = dict(row)
    await attach_sets(db, [(kind, row)], ALL_EXTRAS)
    current = current_values(kind, row)
    owned = (await load_owned(db, [(kind, row)])).get((kind, str(asset_id)), {})
    plan = engine.evaluate(facts_for(kind, row), _manual(rules), trigger="manual", kind=kind)
    pending = plan_changes(kind, str(asset_id), current, plan, owned, {r["id"]: r for r in rules})
    settings = []
    for (key, item), a in sorted(owned.items()):
        if key in ITEM_KEYS:
            cur = (current.get(key) or {}).get(item)
            state = "rule" if cur == a.get("value") else "manual_edit"
        elif key == "tags" or key in SET_KEYS:
            present = _present(current, key, item)
            state = "rule" if present else "manual_edit"
            cur = a.get("value") if present else None
        else:
            cur = norm(key, current.get(key))
            state = "rule" if norm(key, a.get("value")) == cur else "manual_edit"
        settings.append({"action_key": key, "item_key": item or None, "value": a.get("value"),
                         "current": cur, "previous": a.get("previous"), "state": state,
                         "rule_id": a.get("rule_id"), "rule_name": a.get("rule_name"),
                         "run_id": a.get("run_id"), "applied_at": a.get("applied_at")})
    history = [dict(r) for r in (await db.execute(text(
        "SELECT i.run_id::text AS run_id, i.rule_name, i.action_key, i.item_key, i.before, i.after, i.outcome,"
        " i.detail, i.created_at, r.trigger FROM discovery_engine_run_items i"
        " JOIN discovery_engine_runs r ON r.id = i.run_id"
        " WHERE i.asset_kind = :k AND i.asset_id = CAST(:id AS uuid) ORDER BY i.id DESC LIMIT 25"),
        {"k": kind, **params})).mappings().all()]
    return {"asset_kind": kind, "asset_id": str(asset_id), "name": row.get("hostname") or row.get("display_name"),
            "settings": settings, "matching_rules": plan["matched"], "pending": pending, "history": history}


async def conflicts(db: AsyncSession, rules: list[dict], *, limit: int = MAX_ASSETS) -> dict:
    """Over the current inventory: how often each active rule matches and wins,
    which rules override which, and rules that match nothing or never win."""
    assets = await load_assets(db, {}, limit=limit, extras=needs(rules))
    stats = {r["id"]: {"rule_id": r["id"], "rule_name": r["name"], "priority": r["priority"], "matches": 0,
                       "wins": 0, "shadowed": 0} for r in rules}
    pairs: dict = {}
    for kind, row in assets:
        plan = engine.evaluate(facts_for(kind, row), _manual(rules), trigger="manual", kind=kind)
        for m in plan["matched"]:
            stats[m["rule_id"]]["matches"] += 1
        for key, e in plan["settings"].items():
            if key != "decision":
                stats[e["rule_id"]]["wins"] += 1
        for t in plan["tags"]:
            stats[t["rule_id"]]["wins"] += 1
        for s in plan["shadowed"]:
            if s["key"] == "decision":
                continue
            stats[s["rule_id"]]["shadowed"] += 1
            k = (s["rule_id"], s["winner_rule_id"], s["key"])
            p = pairs.setdefault(k, {"rule_id": s["rule_id"], "rule_name": s["rule_name"],
                                     "winner_rule_id": s["winner_rule_id"], "winner_rule_name": s["winner_rule_name"],
                                     "action_key": s["key"], "assets": 0})
            p["assets"] += 1
    out = sorted(stats.values(), key=lambda s: s["priority"])
    return {"assets": len(assets), "truncated": len(assets) >= limit, "rules": out,
            "overridden": sorted(pairs.values(), key=lambda p: -p["assets"]),
            "never_match": [s for s in out if s["matches"] == 0],
            "never_win": [s for s in out if s["matches"] and not s["wins"]]}


# ── "when added" sweep and simulation ───────────────────────────────────────

async def _created_since(db: AsyncSession) -> datetime:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": STATE_KEY})).first()
    value = row[0] if row else None
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and value.get("created_since"):
        return datetime.fromisoformat(str(value["created_since"]).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    await db.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
                          " ON CONFLICT (key) DO NOTHING"),
                     {"k": STATE_KEY, "v": json.dumps({"created_since": now.isoformat()})})
    return now


async def record_simulation(db: AsyncSession, assets: list[tuple[str, dict]], active: list[dict],
                            simulating: list[dict], owned: dict, *, trigger: str) -> int:
    """What the rules in Simulating mode would change, recorded as
    ``simulated`` items of a run in mode ``simulate``. Writes nothing else."""
    if not simulating or not assets:
        return 0
    sim_ids = {r["id"] for r in simulating}
    changes, _, _ = evaluate_assets(assets, active + simulating, owned, trigger=trigger)
    changes = [c for c in changes if c["rule_id"] in sim_ids and c["outcome"] == "apply"]
    if not changes:
        return 0
    run = DiscoveryEngineRun(trigger=trigger, mode="simulate", status="completed",
                             rules_revision=engine.rules_revision(active + simulating),
                             counts={"simulated": len(changes)}, finished_at=datetime.now(timezone.utc))
    db.add(run)
    await db.flush()
    names = {(k, str(r["id"])): r.get("ip_address") or r.get("primary_ip") for k, r in assets}
    for c in changes:
        c["ip_address"] = names.get((c["asset_kind"], c["asset_id"]))
        db.add(_item(run.id, c, "simulated"))
    return len(changes)


async def sweep_created(db: AsyncSession, *, batch: int = 200, apply_credential=None) -> dict:
    """Evaluate rules with the ``created`` trigger on devices and servers added
    since the upgrade, each once. While the engine is off new assets are only
    marked as seen, so turning it on never reaches back. Commits."""
    from .runner import load_rules, load_settings

    since = await _created_since(db)
    assets: list[tuple[str, dict]] = []
    for kind, base, alias in (("device", DEVICE_SQL, "d"), ("child", CHILD_SQL, "d"), ("server", SERVER_SQL, "s")):
        rows = (await db.execute(text(
            base + f" AND {alias}.created_at >= :since AND NOT EXISTS (SELECT 1 FROM discovery_engine_asset_state st"
            f" WHERE st.asset_kind = :k AND st.asset_id = {alias}.id) ORDER BY {alias}.created_at LIMIT :lim"),
            {"since": since, "k": kind, "lim": batch})).mappings().all()
        assets.extend((kind, dict(r)) for r in rows)
    if not assets:
        return {"assets": 0}
    settings = await load_settings(db)
    out: dict[str, Any] = {"assets": len(assets), "enabled": bool(settings["enabled"])}
    matched_by: dict = {}
    revision = None
    if settings["enabled"]:
        await db.execute(text(APPLY_LOCK))
        every = await load_rules(db, modes=("active", "simulate"))
        created = [r for r in every if "created" in (r.get("triggers") or [])]
        active = [r for r in created if r["mode"] == "active"]
        simulating = [r for r in created if r["mode"] == "simulate"]
        await attach_sets(db, assets, needs(active + simulating))
        revision = engine.rules_revision(active)
        owned = await load_owned(db, assets)
        changes, matched, plans = evaluate_assets(assets, active, owned, trigger="created")
        matched_by = {k: [uuid.UUID(m["rule_id"]) for m in p["matched"]] for k, p in plans.items()}
        if changes:
            run = DiscoveryEngineRun(trigger="created", mode="apply", rules_revision=revision, status="running",
                                     targets={"assets": len(assets)})
            db.add(run)
            await db.flush()
            counts = await apply_changes(db, changes, run, apply_credential=apply_credential)
            run.counts = {**summarise(changes, len(assets), matched), **counts}
            run.status = "partial" if counts["skipped_changed"] else "completed"
            run.finished_at = datetime.now(timezone.utc)
            out.update(counts)
        out["simulated"] = await record_simulation(db, assets, active, simulating, owned, trigger="created")
    now = datetime.now(timezone.utc)
    for kind, row in assets:
        await db.execute(text(
            "INSERT INTO discovery_engine_asset_state (asset_kind, asset_id, created_evaluated_at, evaluated_at,"
            " rules_revision, matched_rule_ids) VALUES (:k, :id, :now, :ev, :rev, CAST(:m AS uuid[]))"
            " ON CONFLICT (asset_kind, asset_id) DO NOTHING"),
            {"k": kind, "id": row["id"], "now": now, "ev": now if settings["enabled"] else None, "rev": revision,
             "m": [str(x) for x in matched_by.get((kind, str(row["id"])), [])] or None})
    await db.execute(text(
        "DELETE FROM discovery_engine_assignments a WHERE (a.asset_kind = 'device' AND NOT EXISTS"
        " (SELECT 1 FROM devices d WHERE d.id = a.asset_id)) OR (a.asset_kind = 'server' AND NOT EXISTS"
        " (SELECT 1 FROM servers s WHERE s.id = a.asset_id))"))
    await db.commit()
    return out
