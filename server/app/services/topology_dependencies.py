"""Parent/child dependencies (alerting plan G-13; docs/dependencies.md).

A ``topology_dependencies`` row says "this child sits behind that parent": the
device status, service-check, trap and SNMP evaluators record a child's alert
as suppressed while an enabled, suppressing parent is down, degraded or
unknown, and server availability (``server_availability``) does the same for a
device -> server row.

Ownership (``source``, migrate-203): ``manual`` and ``topology`` rows (added on
the Dependencies page, the latter from a topology suggestion) belong to the
operator; ``discovery_rule`` and ``controller`` rows belong to those engines,
which add and remove them, and are never deleted here.

Suggestions come from the current LLDP/CDP topology only
(``map_topology.build_adjacencies``: seen within 24 h, both ports present and
up): the device nearest the appliance is the root of its part of the network,
and every other device's parent is its neighbour one hop closer to the root. A
device with two such neighbours (redundant uplinks) gets no suggestion: one
parent going down does not cut it off. Nothing is ever created automatically.

The root of each connected part is, in this order: the device the operator
picked; the device whose address is the appliance's default gateway; the
device with the lowest ping round-trip time. A part with none of those gets no
suggestions.
"""
from __future__ import annotations

import socket
import struct
from collections import deque
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping

OPERATOR_SOURCES = ("manual", "topology")
ENGINE_SOURCES = ("discovery_rule", "controller")
TYPES = ("uplink", "wan", "power", "site", "service", "manual")
SUPPRESSING_PARENT_STATUSES = ("down", "degraded", "unknown")
LOCK_KEY = "zenplus:topology-dependencies"

SOURCE_LABELS = {"manual": "Added by hand", "topology": "Accepted topology suggestion",
                 "discovery_rule": "Discovery rule", "controller": "Controller sync"}


class DependencyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ─── Reading ─────────────────────────────────────────────────────────────────

_LIST_SQL = """
SELECT td.id::text AS id, td.dependency_type, td.suppress_alerts, td.enabled, td.notes, td.source,
       td.created_at, u.username AS created_by,
       p.id::text AS parent_id, p.hostname AS parent_name, host(p.ip_address) AS parent_ip,
       p.status AS parent_status, p.device_type AS parent_type, p.tags AS parent_tags,
       CASE WHEN td.child_server_id IS NULL THEN 'device' ELSE 'server' END AS child_kind,
       COALESCE(cd.id, cs.id)::text AS child_id,
       COALESCE(cd.hostname, cs.hostname, cs.display_name) AS child_name,
       COALESCE(host(cd.ip_address), host(cs.primary_ip)) AS child_ip,
       COALESCE(cd.status, cs.status) AS child_status,
       COALESCE(cd.device_type, 'server') AS child_type,
       COALESCE(cd.tags, cs.tags) AS child_tags
FROM topology_dependencies td
JOIN devices p ON p.id = td.parent_device_id
LEFT JOIN devices cd ON cd.id = td.child_device_id
LEFT JOIN servers cs ON cs.id = td.child_server_id
LEFT JOIN users u ON u.id = td.created_by
WHERE {where}
ORDER BY lower(p.hostname), lower(COALESCE(cd.hostname, cs.hostname, cs.display_name, ''))
"""


def _row(r: dict) -> dict:
    return {
        "id": r["id"], "dependency_type": r["dependency_type"], "suppress_alerts": bool(r["suppress_alerts"]),
        "enabled": bool(r["enabled"]), "notes": r["notes"], "source": r["source"],
        "source_label": SOURCE_LABELS.get(r["source"], r["source"]),
        "removable": r["source"] in OPERATOR_SOURCES,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None, "created_by": r["created_by"],
        "parent": {"kind": "device", "id": r["parent_id"], "name": r["parent_name"], "ip": r["parent_ip"],
                   "status": r["parent_status"], "type": r["parent_type"]},
        "child": {"kind": r["child_kind"], "id": r["child_id"], "name": r["child_name"], "ip": r["child_ip"],
                  "status": r["child_status"], "type": r["child_type"]},
    }


async def list_dependencies(db: AsyncSession, scope: Optional[list[str]], *,
                            device_id: Optional[str] = None, server_id: Optional[str] = None) -> list[dict]:
    """Every dependency whose parent and child the caller can see."""
    where = ("(CAST(:device_id AS uuid) IS NULL OR td.parent_device_id = CAST(:device_id AS uuid)"
             " OR td.child_device_id = CAST(:device_id AS uuid))"
             " AND (CAST(:server_id AS uuid) IS NULL OR td.child_server_id = CAST(:server_id AS uuid))")
    rows = (await db.execute(text(_LIST_SQL.format(where=where)),
                             {"device_id": device_id, "server_id": server_id})).mappings().all()
    return [_row(r) for r in rows
            if scoping.entity_visible(r["parent_tags"], scope) and scoping.entity_visible(r["child_tags"], scope)]


# ─── Writing ─────────────────────────────────────────────────────────────────

async def _asset(db: AsyncSession, kind: str, asset_id: str) -> Optional[dict]:
    if kind == "device":
        sql = ("SELECT id::text, hostname AS name, tags, managed_by_device_id::text AS managed_by "
               "FROM devices WHERE id = CAST(:id AS uuid)")
    else:
        sql = ("SELECT id::text, COALESCE(hostname, display_name) AS name, tags, NULL AS managed_by "
               "FROM servers WHERE id = CAST(:id AS uuid)")
    r = (await db.execute(text(sql), {"id": asset_id})).mappings().first()
    return dict(r) if r else None


async def would_cycle(db: AsyncSession, parent_id: str, child_device_id: str) -> bool:
    """Whether making ``child`` depend on ``parent`` closes a loop: the parent
    already sits (directly or further down) behind the child."""
    if parent_id == child_device_id:
        return True
    row = (await db.execute(text("""
        WITH RECURSIVE below(id) AS (
            SELECT child_device_id FROM topology_dependencies
             WHERE parent_device_id = CAST(:child AS uuid) AND child_device_id IS NOT NULL
            UNION
            SELECT td.child_device_id FROM topology_dependencies td JOIN below b ON td.parent_device_id = b.id
             WHERE td.child_device_id IS NOT NULL
        )
        SELECT 1 FROM below WHERE id = CAST(:parent AS uuid) LIMIT 1
    """), {"child": child_device_id, "parent": parent_id})).first()
    return row is not None


async def create(db: AsyncSession, *, parent_device_id: str, child_kind: str, child_id: str,
                 dependency_type: str = "uplink", notes: Optional[str] = None, source: str = "manual",
                 user_id: Optional[str] = None, scope: Optional[list[str]] = None) -> dict:
    """Add one operator-owned dependency (no commit). Raises DependencyError."""
    if source not in OPERATOR_SOURCES:
        raise DependencyError(422, "Only manual and topology dependencies can be added here")
    if dependency_type not in TYPES:
        raise DependencyError(422, f"dependency_type must be one of {', '.join(TYPES)}")
    if child_kind not in ("device", "server"):
        raise DependencyError(422, "The child must be a device or a server")
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK_KEY})
    parent = await _asset(db, "device", parent_device_id)
    child = await _asset(db, child_kind, child_id)
    if parent is None or child is None:
        raise DependencyError(404, "The parent device or the child was not found")
    if not scoping.entity_visible(parent["tags"], scope) or not scoping.entity_visible(child["tags"], scope):
        raise DependencyError(403, "Both the parent and the child must be in your scope")
    col = "child_device_id" if child_kind == "device" else "child_server_id"
    existing = (await db.execute(text(
        f"SELECT source FROM topology_dependencies WHERE parent_device_id = CAST(:p AS uuid) "
        f"AND {col} = CAST(:c AS uuid) LIMIT 1"), {"p": parent_device_id, "c": child_id})).first()
    if existing:
        raise DependencyError(409, f"{child['name']} already depends on {parent['name']} "
                                   f"({SOURCE_LABELS.get(existing[0], existing[0]).lower()})")
    if child_kind == "device" and await would_cycle(db, parent_device_id, child_id):
        raise DependencyError(409, f"{parent['name']} already depends on {child['name']}: "
                                   f"this dependency would make a loop")
    row = (await db.execute(text(
        f"INSERT INTO topology_dependencies (parent_device_id, {col}, dependency_type, suppress_alerts, "
        f"enabled, notes, source, created_by) VALUES (CAST(:p AS uuid), CAST(:c AS uuid), :t, true, true, "
        f":n, :s, CAST(:u AS uuid)) RETURNING id::text"),
        {"p": parent_device_id, "c": child_id, "t": dependency_type, "n": (notes or None),
         "s": source, "u": user_id})).first()
    return {"id": row[0], "parent": parent, "child": {**child, "kind": child_kind}}


async def delete(db: AsyncSession, dep_id: str, scope: Optional[list[str]] = None) -> dict:
    """Remove one operator-owned dependency (no commit). Raises DependencyError."""
    r = (await db.execute(text(_LIST_SQL.format(where="td.id = CAST(:dep AS uuid)")),
                          {"dep": dep_id})).mappings().first()
    if r is None:
        raise DependencyError(404, "Dependency not found")
    if not scoping.entity_visible(r["parent_tags"], scope) or not scoping.entity_visible(r["child_tags"], scope):
        raise DependencyError(404, "Dependency not found")
    if r["source"] not in OPERATOR_SOURCES:
        raise DependencyError(409, f"This dependency belongs to the {SOURCE_LABELS[r['source']].lower()}; "
                                   f"change it there")
    await db.execute(text("DELETE FROM topology_dependencies WHERE id = CAST(:id AS uuid)"), {"id": dep_id})
    return _row(r)


# ─── Suggestions ─────────────────────────────────────────────────────────────

def default_gateways(path: str = "/proc/net/route") -> list[str]:
    """IPv4 default gateways of this appliance (empty when unreadable)."""
    out: list[str] = []
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                f = line.split()
                if len(f) < 4 or f[1] != "00000000" or not int(f[3], 16) & 0x2:
                    continue
                ip = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
                if ip not in out:
                    out.append(ip)
    except (OSError, ValueError):
        return []
    return out


def plan(nodes: dict[str, dict], pairs: Iterable[frozenset], *, root: Optional[str] = None,
         gateways: Iterable[str] = ()) -> dict:
    """Parents from the adjacency graph.

    ``nodes``: {device id: {ip, last_rtt_ms}}; ``pairs``: fresh adjacencies.
    Returns {roots: [(id, reason)], parents: {child: (parent, hops)},
    redundant: {child: [parents]}, unrooted: [component ids]}."""
    nbrs: dict[str, set[str]] = {i: set() for i in nodes}
    for p in pairs:
        if len(p) != 2:
            continue
        a, b = tuple(p)
        if a in nbrs and b in nbrs:
            nbrs[a].add(b)
            nbrs[b].add(a)
    gw = set(gateways)
    seen: set[str] = set()
    out = {"roots": [], "parents": {}, "redundant": {}, "unrooted": []}
    for start in sorted(nbrs):
        if start in seen or not nbrs[start]:
            continue
        comp, queue = {start}, deque([start])
        while queue:
            for n in nbrs[queue.popleft()]:
                if n not in comp:
                    comp.add(n)
                    queue.append(n)
        seen |= comp
        if root is not None:
            if root not in comp:
                continue
            chosen, reason = root, "chosen"
        else:
            at_gw = sorted(i for i in comp if nodes[i].get("ip") in gw)
            rtts = sorted((nodes[i]["last_rtt_ms"], i) for i in comp if nodes[i].get("last_rtt_ms") is not None)
            if at_gw:
                chosen, reason = at_gw[0], "gateway"
            elif rtts:
                chosen, reason = rtts[0][1], "lowest_rtt"
            else:
                out["unrooted"].append(sorted(comp))
                continue
        out["roots"].append((chosen, reason))
        dist = {chosen: 0}
        queue = deque([chosen])
        while queue:
            cur = queue.popleft()
            for n in sorted(nbrs[cur]):
                if n not in dist:
                    dist[n] = dist[cur] + 1
                    queue.append(n)
        for node, d in dist.items():
            if d == 0:
                continue
            ups = sorted(n for n in nbrs[node] if dist.get(n) == d - 1)
            if len(ups) == 1:
                out["parents"][node] = (ups[0], d)
            else:
                out["redundant"][node] = ups
    return out


def _brief(d: dict) -> dict:
    return {"kind": "device", "id": d["id"], "name": d["hostname"], "ip": d["ip"], "status": d["status"],
            "type": d["device_type"]}


async def suggest(db: AsyncSession, scope: Optional[list[str]], *, root_device_id: Optional[str] = None,
                  gateways: Optional[Iterable[str]] = None) -> dict:
    """Suggested device -> device dependencies from the current topology."""
    from app.api.v1.manual_maps import _load_topology
    from app.services import map_topology as topo
    devices = {r["id"]: dict(r) for r in (await db.execute(text(
        "SELECT id::text, hostname, host(ip_address) AS ip, status, device_type, tags, last_rtt_ms "
        "FROM devices WHERE managed_by_device_id IS NULL"))).mappings().all()}
    gws = list(default_gateways() if gateways is None else gateways)
    empty = {"roots": [], "suggestions": [], "redundant": [], "unrooted_components": 0, "gateways": gws}
    if not devices:
        return empty
    edges, ifaces, hostnames = await _load_topology(db, list(devices))
    adjacency = topo.build_adjacencies(edges, ifaces, hostnames)
    fresh = topo.fresh_pairs(adjacency)
    nodes = {i: {"ip": d["ip"], "last_rtt_ms": d["last_rtt_ms"]} for i, d in devices.items()}
    if root_device_id and root_device_id not in nodes:
        raise DependencyError(404, "The starting device was not found")
    p = plan(nodes, fresh, root=root_device_id, gateways=gws)
    linked = {frozenset((str(a), str(b))) for a, b in (await db.execute(text(
        "SELECT parent_device_id, child_device_id FROM topology_dependencies "
        "WHERE child_device_id IS NOT NULL"))).all()}

    def visible(i: str) -> bool:
        return scoping.entity_visible(devices[i]["tags"], scope)

    suggestions = []
    for child, (parent, hops) in sorted(p["parents"].items(),
                                        key=lambda kv: (kv[1][1], (devices[kv[0]]["hostname"] or "").lower())):
        if frozenset((child, parent)) in linked or not (visible(child) and visible(parent)):
            continue
        link = topo.summarize_pair(adjacency.get(frozenset((child, parent)), []), parent) \
            if adjacency.get(frozenset((child, parent))) else {}
        suggestions.append({
            "parent": _brief(devices[parent]), "child": _brief(devices[child]), "hops": hops,
            "parent_interface": link.get("src_interface"), "child_interface": link.get("dst_interface"),
            "protocol": link.get("protocol"), "last_seen_at": link.get("last_seen_at"),
        })
    redundant = [{"child": _brief(devices[c]), "parents": [_brief(devices[x]) for x in ups]}
                 for c, ups in sorted(p["redundant"].items()) if visible(c)]
    return {
        "roots": [{**_brief(devices[r]), "reason": why} for r, why in p["roots"] if visible(r)],
        "suggestions": suggestions, "redundant": redundant,
        "unrooted_components": len(p["unrooted"]), "gateways": gws,
    }
