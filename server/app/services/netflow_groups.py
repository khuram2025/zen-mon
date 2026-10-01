"""Managed IP groups for NetFlow (CR-13).

Named sets of CIDRs ("Datacenter A", "Guest Wi-Fi", ...) used to label hosts,
filter flows (``group=``) and break traffic down by site. Groups live in
PostgreSQL (``netflow_ip_groups``, migrate-138); B7's settings API edits them.

Appliances that configured groups before the table existed kept them in a
JSON file (``NETFLOW_IP_GROUPS_FILE``, default
``/opt/zenplus/data/netflow-ip-groups.json``). The first load that finds the
table empty imports that file once and records the import in
``system_settings`` (``netflow_ip_groups_import``), so deleting every group
later does not resurrect the file's contents.

Membership semantics:

* a group's addresses are its own CIDRs plus those of its descendants
  (``parent_id``), so filtering on "Datacenter A" includes its sub-groups;
* ``classify_ip`` names the most specific group (longest prefix, then name),
  so a host in a sub-group is labelled with the sub-group.

SQL predicates follow the schema v2 address contract (§3.2): an IPv4 CIDR is
a range test on the IPv4 column restricted to rows whose IPv6 twin is ``::``
(so the primary-key-friendly IPv4 column is used and ``toString`` is never
applied per row); an IPv6 CIDR is a range test on the unified address.
Column arguments name the IPv4 column; its IPv6 twin is the same name plus
``6`` (``src_addr`` / ``src_addr6``), true for every flow table and rollup.

The sync readers (``group_names``, ``group_predicate``, ``classify_ip``) use
the cached snapshot; request handlers call ``await load_groups()`` first
(cheap while the cache is fresh) so the snapshot reflects the database.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

IP_GROUPS_FILE_ENV = "NETFLOW_IP_GROUPS_FILE"
DEFAULT_IP_GROUPS_FILE = "/opt/zenplus/data/netflow-ip-groups.json"
IMPORT_MARKER_KEY = "netflow_ip_groups_import"
CACHE_TTL_S = 60.0
NAME_MAX = 120

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_IPV4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")


def ip_groups_file() -> str:
    return os.getenv(IP_GROUPS_FILE_ENV, DEFAULT_IP_GROUPS_FILE)


# ── Address SQL (schema v2, contract §3.2) ──────────────────────────────────

def v6_twin(v4_col: str) -> str:
    """The IPv6 column paired with an IPv4 address column."""
    return f"{v4_col}6"


_FLOW_PAIR = {"src_addr": "dst_addr", "dst_addr": "src_addr"}


def v4_row_sql(v4_col: str) -> str:
    """Predicate: the address in (v4_col, v4_col+'6') is IPv4.

    For a flow's endpoints this is the row's family (both IPv6 twins '::'),
    not the one side's column: an IPv6 flow from the unspecified address '::'
    (DAD, MLD) is not the IPv4 address 0.0.0.0 (NF-32, sql.IS_V4_FLOW)."""
    v6 = v6_twin(v4_col)
    prefix, dot, bare = v4_col.rpartition(".")
    other = _FLOW_PAIR.get(bare)
    if other:
        return f"({v6} = toIPv6('::') AND {v6_twin(prefix + dot + other)} = toIPv6('::'))"
    return f"{v6} = toIPv6('::')"


def unified_addr_sql(v4_col: str) -> str:
    """One IPv6-typed address from the IPv4/IPv6 column pair (IPv4 mapped)."""
    v6 = v6_twin(v4_col)
    return f"if({v4_row_sql(v4_col)}, IPv4ToIPv6({v4_col}), {v6})"


def parse_network(value) -> Network | None:
    """A CIDR or bare address as a network (host bits dropped), or None."""
    try:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return None
    if isinstance(net, ipaddress.IPv6Network) and net.subnet_of(_IPV4_MAPPED) and net.prefixlen >= 96:
        # ::ffff:10.0.0.0/104 is an IPv4 range; flows store IPv4 in the IPv4
        # column, so test it there.
        net = ipaddress.IPv4Network(
            (int(net.network_address) & 0xFFFFFFFF, net.prefixlen - 96)
        )
    return net


def cidr_sql(net: Network, v4_col: str) -> str:
    """Predicate: the address held in (v4_col, v4_col+'6') is inside ``net``.

    Literals are generated from a parsed network, never from user text, so
    inlining them is injection-safe.
    """
    lo, hi = net.network_address, net.broadcast_address
    if isinstance(net, ipaddress.IPv4Network):
        return (
            f"({v4_row_sql(v4_col)} AND "
            f"{v4_col} BETWEEN toIPv4('{lo}') AND toIPv4('{hi}'))"
        )
    return f"({unified_addr_sql(v4_col)} BETWEEN toIPv6('{lo}') AND toIPv6('{hi}'))"


def networks_sql(nets: Iterable[Network], v4_col: str) -> str | None:
    """OR of ``cidr_sql`` over several networks; None when there are none."""
    terms = [cidr_sql(n, v4_col) for n in _collapse(nets)]
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def _collapse(nets: Iterable[Network]) -> list[Network]:
    v4 = [n for n in nets if isinstance(n, ipaddress.IPv4Network)]
    v6 = [n for n in nets if isinstance(n, ipaddress.IPv6Network)]
    return list(ipaddress.collapse_addresses(v4)) + list(ipaddress.collapse_addresses(v6))


def ip_in(ip: str, nets: Iterable[Network]) -> bool:
    """Python mirror of ``networks_sql``: IPv4 is compared as IPv4, and as its
    IPv4-mapped form (``::ffff:a.b.c.d``, the unified expression's value)
    against IPv6 networks -- so ``::/0`` covers IPv4 flows in both."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    mapped = (ipaddress.IPv6Address(int(addr) | 0xFFFF00000000)
              if isinstance(addr, ipaddress.IPv4Address) else addr)
    for n in nets:
        if (addr in n) if addr.version == n.version else (mapped in n):
            return True
    return False


# ── Model ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Group:
    id: str | None
    name: str
    cidrs: tuple[Network, ...]
    description: str | None = None
    color: str | None = None
    tags: tuple[str, ...] = ()
    parent_id: str | None = None
    # Own CIDRs plus every descendant's: what "in this group" means.
    effective: tuple[Network, ...] = field(default_factory=tuple)


@dataclass
class _State:
    groups: list[Group] = field(default_factory=list)
    by_name: dict[str, Group] = field(default_factory=dict)
    loaded_at: float = 0.0
    generation: int = 0
    loaded: bool = False


_state = _State()
_lock = threading.Lock()


def invalidate_groups_cache() -> None:
    """Force the next ``load_groups`` to re-read the table (B7 calls this
    after every IP-group write)."""
    with _lock:
        _state.generation += 1
        _state.loaded_at = 0.0


def _with_effective(groups: list[Group]) -> list[Group]:
    children: dict[str, list[Group]] = {}
    for g in groups:
        if g.parent_id:
            children.setdefault(g.parent_id, []).append(g)

    def gather(g: Group, seen: set[str]) -> list[Network]:
        nets = list(g.cidrs)
        if g.id is None or g.id in seen:
            return nets          # cycle guard: a parent loop cannot recurse forever
        seen = seen | {g.id}
        for child in children.get(g.id, []):
            nets.extend(gather(child, seen))
        return nets

    out = []
    for g in groups:
        eff = tuple(_collapse(gather(g, set())))
        out.append(Group(g.id, g.name, g.cidrs, g.description, g.color, g.tags, g.parent_id, eff))
    return out


def _set_groups(groups: list[Group], generation: int) -> None:
    groups = sorted(_with_effective(groups), key=lambda g: g.name.lower())
    with _lock:
        if _state.generation != generation:
            return   # invalidated while we were reading; the next load re-reads
        _state.groups = groups
        _state.by_name = {g.name: g for g in groups}
        _state.loaded_at = time.monotonic()
        _state.loaded = True


# ── Loading and the one-time JSON import ────────────────────────────────────

def read_groups_file(path: str | None = None) -> tuple[list[dict], list[str]]:
    """Parse the legacy JSON file into ``[{name, cidrs, description, color}]``.

    Returns (groups, problems). Invalid CIDRs and nameless or empty groups are
    skipped and reported; a later duplicate name merges its CIDRs into the
    first so nothing the operator listed is silently dropped.
    """
    path = path or ip_groups_file()
    problems: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [f"{path}: unreadable ({exc.__class__.__name__})"]
    if not isinstance(raw, list):
        return [], [f"{path}: expected a JSON list of groups"]
    merged: dict[str, dict] = {}
    for i, g in enumerate(raw):
        if not isinstance(g, dict):
            problems.append(f"entry {i}: not an object")
            continue
        name = str(g.get("name") or "").strip()[:NAME_MAX]
        if not name:
            problems.append(f"entry {i}: missing name")
            continue
        cidrs: list[str] = []
        for c in g.get("cidrs") or []:
            net = parse_network(c)
            if net is None:
                problems.append(f"{name}: invalid CIDR {c!r}")
                continue
            cidrs.append(str(net))
        entry = merged.setdefault(name, {
            "name": name, "cidrs": [],
            "description": (str(g["description"]).strip() or None) if g.get("description") else None,
            "color": (str(g["color"]).strip()[:16] or None) if g.get("color") else None,
        })
        for c in cidrs:
            if c not in entry["cidrs"]:
                entry["cidrs"].append(c)
    out = []
    for entry in merged.values():
        if entry["cidrs"]:
            out.append(entry)
        else:
            problems.append(f"{entry['name']}: no valid CIDRs")
    return out, problems


async def import_groups_file(db: AsyncSession, path: str | None = None) -> dict | None:
    """Import the legacy JSON file into an EMPTY table, once.

    Serialized with a transaction-scoped advisory lock so two workers
    starting together cannot both import. Returns the import summary, or
    None when nothing was imported (table not empty, already imported, or
    no file). Commits its own transaction.
    """
    path = path or ip_groups_file()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('netflow_ip_groups_import'))"))
    marker = (await db.execute(
        text("SELECT 1 FROM system_settings WHERE key = :k"), {"k": IMPORT_MARKER_KEY}
    )).first()
    has_rows = (await db.execute(text("SELECT 1 FROM netflow_ip_groups LIMIT 1"))).first()
    if marker or has_rows:
        await db.commit()
        return None
    groups, problems = read_groups_file(path)
    for g in groups:
        await db.execute(text(
            "INSERT INTO netflow_ip_groups (name, description, cidrs, color) "
            "VALUES (:name, :description, CAST(:cidrs AS text[]), :color) "
            "ON CONFLICT (name) DO NOTHING"
        ), {"name": g["name"], "description": g["description"],
            "cidrs": g["cidrs"], "color": g["color"]})
    summary = {
        "source": path,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "groups": len(groups),
        "problems": problems,
        "file_found": os.path.exists(path),
    }
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO NOTHING"
    ), {"k": IMPORT_MARKER_KEY, "v": json.dumps(summary)})
    await db.commit()
    if groups or problems:
        logger.info("imported %d NetFlow IP groups from %s (%d problems)",
                    len(groups), path, len(problems))
    return summary


async def load_groups(db: AsyncSession | None = None, *, force: bool = False) -> list[Group]:
    """Current groups from PostgreSQL (cached ``CACHE_TTL_S``).

    The first load on an appliance whose table is empty imports the legacy
    JSON file. If the table does not exist yet (code running ahead of
    migrate-138) the cached snapshot -- empty on a fresh process -- is kept.

    Request handlers pass no session: loading uses its own short-lived one,
    because the one-time import commits and a failed read rolls back, and
    neither may touch the caller's transaction. Tests and scripts may pass
    an explicit session, which is then committed/rolled back as described.
    """
    with _lock:
        fresh = _state.loaded and time.monotonic() - _state.loaded_at < CACHE_TTL_S
        generation = _state.generation
        if fresh and not force:
            return list(_state.groups)
    if db is None:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as own:
            return await _load(own, generation)
    return await _load(db, generation)


async def _load(db: AsyncSession, generation: int) -> list[Group]:
    try:
        if not _state.loaded:
            await import_groups_file(db)
        rows = (await db.execute(text(
            "SELECT id::text AS id, name, description, cidrs, color, tags, parent_id::text AS parent_id "
            "FROM netflow_ip_groups ORDER BY name"
        ))).all()
    except Exception:
        logger.warning("netflow_ip_groups unavailable; keeping the cached IP groups", exc_info=True)
        await db.rollback()
        return groups_snapshot()
    groups = []
    for r in rows:
        nets = tuple(n for n in (parse_network(c) for c in (r.cidrs or [])) if n is not None)
        tags = r.tags if isinstance(r.tags, list) else (json.loads(r.tags) if isinstance(r.tags, str) else [])
        groups.append(Group(
            id=r.id, name=r.name, cidrs=nets, description=r.description, color=r.color,
            tags=tuple(str(t) for t in tags if t), parent_id=r.parent_id,
        ))
    _set_groups(groups, generation)
    return groups_snapshot()


def set_groups_for_tests(groups: list[Group]) -> None:
    """Install a snapshot without a database (unit tests)."""
    with _lock:
        generation = _state.generation
    _set_groups(groups, generation)


# ── Sync readers over the snapshot ──────────────────────────────────────────

def groups_snapshot() -> list[Group]:
    with _lock:
        return list(_state.groups)


def group_names() -> list[str]:
    with _lock:
        return [g.name for g in _state.groups]


def get_group(name: str) -> Group | None:
    with _lock:
        return _state.by_name.get(name)


def group_predicate(name: str, src_expr: str = "src_addr", dst_expr: str = "dst_addr") -> str | None:
    """ClickHouse predicate: the flow's source OR destination is in the group
    (including sub-groups). None when the group does not exist -- callers
    must reject the filter (400), never drop it."""
    g = get_group(name)
    if g is None:
        return None
    src = networks_sql(g.effective, src_expr)
    dst = networks_sql(g.effective, dst_expr)
    if src is None or dst is None:
        return "0"       # a group with no addresses matches nothing
    return f"({src} OR {dst})"


def _specificity(net: Network) -> int:
    """Prefix length in the IPv6 space the unified address lives in: an IPv4
    /24 is a /120 there, so it outranks an IPv6 /96 that also covers the
    address through its IPv4-mapped form."""
    return net.prefixlen + (96 if isinstance(net, ipaddress.IPv4Network) else 0)


def classify_ip(ip: str) -> str | None:
    """The most specific group containing ``ip`` (longest prefix, then name)."""
    best: tuple[int, str, str] | None = None
    for g in groups_snapshot():
        for n in g.cidrs:
            if ip_in(ip, (n,)):
                key = (-_specificity(n), g.name.lower(), g.name)
                if best is None or key < best:
                    best = key
    return best[2] if best is not None else None


def classify_ips(ips: Iterable[str]) -> dict[str, str | None]:
    return {ip: classify_ip(ip) for ip in ips}


async def ensure_loaded() -> None:
    """Refresh the snapshot if stale (for request handlers and loops)."""
    await load_groups()


__all__ = [
    "Group", "load_groups", "group_names", "group_predicate", "classify_ip",
    "invalidate_groups_cache", "ensure_loaded", "import_groups_file",
    "read_groups_file", "cidr_sql", "networks_sql", "unified_addr_sql", "v4_row_sql",
    "parse_network", "ip_in", "get_group", "groups_snapshot", "classify_ips",
]
