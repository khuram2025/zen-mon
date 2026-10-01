"""NetFlow visibility: which exporters' flows a user may see (NF-04 / CR-04).

Flow data carries no tags of its own, so visibility is borrowed from the
device that owns each exporter:

* an explicit ``netflow_exporter_devices`` mapping wins (routers commonly
  export from a loopback or WAN address, and an operator who states the owner
  must not be overruled by a coincidental management-IP match);
* otherwise the device whose management IP equals the exporter owns it;
* an exporter is visible to a tag-scoped user iff its owner passes the user's
  scope (``core/scoping.py`` semantics: case-insensitive ANY-of, untagged
  devices hidden);
* an exporter with no owner is hidden from scoped users (fail-closed), exactly
  like an untagged device.

Unrestricted users (empty scope, or a role carrying ``system.admin``) see
every exporter, including unowned ones.

One resolver (``resolve_exporter_owners``) answers "who owns this exporter"
for name resolution, the exporter-device listing and authorization alike, so
what the UI labels an exporter with and who may read its flows cannot drift.

Caching: the owner map and each scope's visible set are cached for 15 s (the
role-permission cache uses the same TTL). Writes that can change ownership --
devices added or deleted, a device's ``tags`` or ``ip_address`` assigned, any
write to ``netflow_exporter_devices`` (``is_ownership_write``) -- invalidate
the cache when their transaction commits. The hook sits on the SQLAlchemy
engine rather than in each writer because tags are written from several
modules (device CRUD, bulk tagging, tag rename/delete) through both the ORM
and raw SQL; a per-call-site invalidation would silently miss the next writer
someone adds. The cache is per process: a second uvicorn worker converges
within the TTL, and writers outside this process (the poller) do not touch
tags, management IPs or mappings.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable

from fastapi import Depends, HTTPException
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import SUPERUSER_PERMISSION
from app.core.security import get_role_permissions, require_permission
from app.models.user import User
from app.services.tag_service import tag_set

logger = logging.getLogger(__name__)

VIEW_PERMISSION = "netflow.view"
MANAGE_PERMISSION = "netflow.manage"
EXPORT_PERMISSION = "netflow.export"

CACHE_TTL_S = 15.0

# Route-level permission gates (contract §6). ``system.admin`` always passes.
require_view = require_permission(VIEW_PERMISSION)
require_manage = require_permission(MANAGE_PERMISSION)
require_export = require_permission(EXPORT_PERMISSION)


# ── Canonical addresses ─────────────────────────────────────────────────────

def canonical_ip(value) -> str | None:
    """Canonical text for an exporter/host address, or None when unparsable.

    Accepts plain addresses, PostgreSQL INET text (``10.0.0.1/32``) and
    IPv4-mapped IPv6 (``::ffff:10.0.0.1``, how ClickHouse renders an IPv4
    held in an IPv6 column); IPv4 always comes back unmapped and IPv6
    compressed, so the same exporter has one spelling across PG, ClickHouse
    and the URL.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def is_ipv6(ip: str) -> bool:
    return ":" in ip


# ── Data shapes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExporterOwner:
    """The device an exporter belongs to."""

    exporter: str                    # canonical IP text
    device_id: str
    hostname: str | None
    source: str                      # 'mapped' | 'ip-match'
    tags: frozenset[str] = field(default_factory=frozenset)   # lowercased
    # A mapping that overrides a device whose management IP is this same
    # address; surfaced so the UI can explain why the ip-match lost.
    overrides_device_id: str | None = None


@dataclass(frozen=True)
class FlowVisibility:
    """The caller's view of the flow data (contract §6)."""

    restricted: bool
    exporters: frozenset[str] | None   # canonical IP text; None = unrestricted
    user_id: str | None
    permissions: frozenset[str]
    # The caller's tag scope (lowercased; None = unrestricted). Lets a check
    # compare scopes, e.g. whether a shared view's owner sees no more than
    # the reader (saved_views.can_read).
    scope: frozenset[str] | None = None

    def allows(self, exporter) -> bool:
        """Whether flows from this exporter may be shown to the caller."""
        if self.exporters is None:
            return True
        ip = canonical_ip(exporter)
        return ip is not None and ip in self.exporters

    def can(self, permission: str) -> bool:
        """Role check; ``system.admin`` implies every permission."""
        return SUPERUSER_PERMISSION in self.permissions or permission in self.permissions

    @property
    def is_empty(self) -> bool:
        """True when the caller is restricted to no exporter at all. Queries
        must return empty results then -- never fall back to unfiltered."""
        return self.exporters is not None and not self.exporters

    def exporter_list(self) -> list[str] | None:
        """Sorted visible exporters for SQL binding; None when unrestricted."""
        return None if self.exporters is None else sorted(self.exporters)


UNRESTRICTED_SYSTEM = FlowVisibility(
    restricted=False, exporters=None, user_id=None,
    permissions=frozenset({SUPERUSER_PERMISSION}),
)
"""Visibility for background jobs acting on behalf of the appliance."""


# ── Cache ───────────────────────────────────────────────────────────────────

class _Cache:
    """Owner map + per-scope visible sets with a generation counter.

    ``generation`` is bumped on every invalidation; a computation that
    started under an older generation does not store its result, so a read
    racing a committing write cannot re-cache the pre-write state for a
    full TTL.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0
        self.owners: tuple[float, int, dict[str, ExporterOwner]] | None = None
        self.scopes: dict[tuple[str, ...], tuple[float, int, frozenset[str]]] = {}

    def invalidate(self) -> None:
        with self.lock:
            self.generation += 1
            self.owners = None
            self.scopes.clear()


_cache = _Cache()


def invalidate_visibility_cache() -> None:
    """Drop cached ownership and visible sets (call after mapping/tag writes;
    the engine hook below already does this on commit)."""
    _cache.invalidate()


# ── Ownership resolution ────────────────────────────────────────────────────

async def _load_owner_map(db: AsyncSession) -> dict[str, ExporterOwner]:
    owners: dict[str, ExporterOwner] = {}
    ip_match: dict[str, ExporterOwner] = {}
    rows = (await db.execute(text(
        "SELECT host(d.ip_address) AS ip, d.id::text AS id, d.hostname, d.tags "
        "FROM devices d WHERE d.ip_address IS NOT NULL ORDER BY d.id"
    ))).all()
    for r in rows:
        ip = canonical_ip(r.ip)
        # Two devices sharing a management IP (a cloned VM, a stale record):
        # the lowest id owns it, deterministically across processes; an
        # explicit mapping settles it for good.
        if ip is None or ip in ip_match:
            continue
        ip_match[ip] = ExporterOwner(
            exporter=ip, device_id=r.id, hostname=r.hostname,
            source="ip-match", tags=frozenset(tag_set(r.tags)),
        )
    owners.update(ip_match)

    try:
        # Savepoint: on an appliance that has not run migrate-054 yet the
        # table is missing, and the failed statement must not poison the
        # caller's transaction.
        async with db.begin_nested():
            mapped = (await db.execute(text(
                "SELECT host(m.exporter_ip) AS ip, d.id::text AS id, d.hostname, d.tags "
                "FROM netflow_exporter_devices m JOIN devices d ON d.id = m.device_id"
            ))).all()
    except Exception:
        logger.warning("netflow_exporter_devices unavailable; using management-IP ownership only",
                       exc_info=True)
        mapped = []
    for r in mapped:
        ip = canonical_ip(r.ip)
        if ip is None:
            continue
        shadowed = ip_match.get(ip)
        owners[ip] = ExporterOwner(
            exporter=ip, device_id=r.id, hostname=r.hostname, source="mapped",
            tags=frozenset(tag_set(r.tags)),
            overrides_device_id=(
                shadowed.device_id if shadowed and shadowed.device_id != r.id else None
            ),
        )
    return owners


async def _owner_map(db: AsyncSession) -> dict[str, ExporterOwner]:
    now = time.monotonic()
    with _cache.lock:
        hit = _cache.owners
        gen = _cache.generation
    if hit and now - hit[0] < CACHE_TTL_S and hit[1] == gen:
        return hit[2]
    owners = await _load_owner_map(db)
    with _cache.lock:
        if _cache.generation == gen:
            _cache.owners = (now, gen, owners)
    return owners


async def resolve_exporter_owners(
    db: AsyncSession, ips: list[str] | None = None
) -> dict[str, ExporterOwner]:
    """{canonical exporter IP -> owner}. An explicit mapping wins over a
    management-IP match. ``ips=None`` returns every known owner; otherwise
    only the requested addresses that have one (keys are canonical)."""
    owners = await _owner_map(db)
    if ips is None:
        return dict(owners)
    out: dict[str, ExporterOwner] = {}
    for raw in ips:
        ip = canonical_ip(raw)
        if ip is not None and ip in owners:
            out[ip] = owners[ip]
    return out


def device_exporters(owners: dict[str, ExporterOwner], device_id) -> list[str]:
    """Exporters owned by one device (reverse of the owner map), sorted."""
    did = str(device_id)
    return sorted(ip for ip, o in owners.items() if o.device_id == did)


async def _visible_exporters(db: AsyncSession, scope: list[str]) -> frozenset[str]:
    key = tuple(scope)
    now = time.monotonic()
    with _cache.lock:
        hit = _cache.scopes.get(key)
        gen = _cache.generation
    if hit and now - hit[0] < CACHE_TTL_S and hit[1] == gen:
        return hit[2]
    owners = await _owner_map(db)
    wanted = set(scope)
    visible = frozenset(ip for ip, o in owners.items() if o.tags & wanted)
    with _cache.lock:
        if _cache.generation == gen:
            _cache.scopes[key] = (now, gen, visible)
    return visible


# ── Visibility for a user ───────────────────────────────────────────────────

async def visibility_for_user(db: AsyncSession, user: User) -> FlowVisibility:
    """The user's flow visibility, without enforcing ``netflow.view``.

    For consumers outside the /netflow router (link utilization, maps,
    reports) that serve users who may lack the NetFlow permission: check
    ``vis.can(VIEW_PERMISSION)`` before showing any flow-derived value.
    """
    perms = frozenset(await get_role_permissions(db, user.role))
    scope = await scoping.visible_tags(db, user)
    uid = str(user.id) if getattr(user, "id", None) is not None else None
    if scope is None:
        return FlowVisibility(restricted=False, exporters=None, user_id=uid, permissions=perms)
    return FlowVisibility(
        restricted=True,
        exporters=await _visible_exporters(db, scope),
        user_id=uid,
        permissions=perms,
        scope=frozenset(scope),
    )


SCOPE_EMPTY_MESSAGE = ("Your scope includes no flow exporters: none of the devices you can see owns an "
                       "exporter that has sent flows to this appliance")


async def flow_visibility(
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
) -> FlowVisibility:
    """FastAPI dependency for every /netflow read: enforces ``netflow.view``
    (403 otherwise) and returns the caller's exporter visibility.

    A restricted caller whose scope holds no exporter that has ever sent
    flows gets a ``scope_empty`` warning, so the page says so instead of
    describing an empty answer as a collection problem."""
    vis = await visibility_for_user(db, user)
    if vis.restricted:
        flowing = await flow_exporters()
        if flowing is not None and not (vis.exporters or frozenset()) & flowing:
            from .common import current_meta

            current_meta().warn("scope_empty", SCOPE_EMPTY_MESSAGE)
    return vis


# ── Exporters that have sent flows ──────────────────────────────────────────
# Ownership counts any device management IP as a potential exporter; what an
# exporter count, an empty-scope notice or an address-claim check needs is
# the set of addresses that actually delivered flows (flow_exporter_minutes,
# kept as long as its TTL). Cached for a minute; a ClickHouse failure yields
# None ("unknown") and is retried no sooner than 30 s later, so a stalled
# database cannot hold requests here.

FLOW_EXPORTERS_TTL_S = 60.0
FLOW_EXPORTERS_RETRY_S = 30.0
FLOW_EXPORTERS_BUDGET_S = 3.0
_flow_exp_cache: dict = {"value": None, "at": 0.0, "failed_at": 0.0, "lock": None, "loop": None, "key": None}


def _flow_exp_lock():
    import asyncio

    loop = asyncio.get_running_loop()
    if _flow_exp_cache["loop"] is not loop:
        _flow_exp_cache.update(lock=asyncio.Lock(), loop=loop)
    return _flow_exp_cache["lock"]


async def flow_exporters(*, refresh: bool = False) -> frozenset[str] | None:
    """Canonical addresses of every exporter with records in
    ``flow_exporter_minutes``; None when ClickHouse cannot say."""
    from .common import EXPORTER_MINUTES, run_query, table_ref
    from .sql import EXPORTER, display

    table = table_ref(EXPORTER_MINUTES)

    def fresh() -> bool:
        if _flow_exp_cache["key"] != table:
            return False
        now = time.monotonic()
        if _flow_exp_cache["value"] is not None and now - _flow_exp_cache["at"] < FLOW_EXPORTERS_TTL_S:
            return True
        return bool(_flow_exp_cache["failed_at"]) and now - _flow_exp_cache["failed_at"] < FLOW_EXPORTERS_RETRY_S

    if not refresh and fresh():
        return _flow_exp_cache["value"]
    async with _flow_exp_lock():
        if not refresh and fresh():
            return _flow_exp_cache["value"]
        try:
            res = await run_query(
                f"SELECT {display(EXPORTER)} AS e FROM {table} GROUP BY exporter_ip, exporter_ip6",
                {}, budget_s=FLOW_EXPORTERS_BUDGET_S, label="flow-exporters")
        except Exception:  # noqa: BLE001 - unknown, not empty
            logger.warning("netflow: exporter list unavailable", exc_info=True)
            _flow_exp_cache.update(value=None, failed_at=time.monotonic(), key=table)
            return None
        value = frozenset(ip for ip in (canonical_ip(r[0]) for r in res.result_rows) if ip)
        _flow_exp_cache.update(value=value, at=time.monotonic(), failed_at=0.0, key=table)
        return value


def flow_exporter_count(vis: FlowVisibility, flowing: frozenset[str] | None) -> int | None:
    """How many exporters with flows the caller sees (None = unknown or
    unrestricted). Device management IPs that never exported are not counted."""
    if vis.exporters is None or flowing is None:
        return None
    return len(vis.exporters & flowing)


async def authorize_device_address(db: AsyncSession, user: User, address) -> None:
    """A tag-scoped user may not give a device a management address that is
    a flow exporter, or already belongs to a device, outside their scope.

    Ownership falls back to the management-IP match, so creating (or
    re-addressing) a device with an unowned or another team's exporter
    address would otherwise pull that exporter's flows into the caller's
    scope -- the same claim ``authorize_mapping_write`` refuses for explicit
    mappings. Unrestricted users are not limited. 403 when refused; 503 when
    ClickHouse cannot tell whether the address exports (fail closed).
    """
    ip = canonical_ip(address)
    if ip is None:
        return
    vis = await visibility_for_user(db, user)
    if vis.exporters is None or vis.allows(ip):
        return
    owners = await resolve_exporter_owners(db, [ip])
    owner = owners.get(ip)
    if owner is not None and owner.source == "mapped":
        return          # an explicit mapping decides ownership; an IP match cannot take it over
    claimed = owner is not None
    if not claimed:
        flowing = await flow_exporters()
        if flowing is None:
            raise HTTPException(status_code=503, detail=(
                "The flow exporter list cannot be read right now, so this management address cannot be "
                "checked; try again shortly"))
        claimed = ip in flowing
    if claimed:
        raise HTTPException(status_code=403, detail=(
            "This management address belongs to a flow exporter or device outside your scope; "
            "ask an administrator to assign it"))


# ── Detail and write checks ─────────────────────────────────────────────────

def require_exporter_visible(vis: FlowVisibility, exporter) -> str:
    """Canonical exporter address, or 404 when the caller cannot see it.

    404 rather than 403: a scoped user must not be able to confirm that an
    out-of-scope exporter exists by probing addresses (scoping.py rule). A
    malformed address is a 400.
    """
    ip = canonical_ip(exporter)
    if ip is None:
        raise HTTPException(status_code=400, detail="Invalid exporter address")
    if not vis.allows(ip):
        raise HTTPException(status_code=404, detail="Exporter not found")
    return ip


@dataclass(frozen=True)
class VisibleDevice:
    id: str
    hostname: str | None
    ip: str | None
    tags: frozenset[str]


async def require_device_visible(db: AsyncSession, user: User, device_id) -> VisibleDevice:
    """The device, or 404 when it does not exist or is outside the caller's
    tag scope (same answer for both, so ids cannot be probed)."""
    try:
        did = uuid.UUID(str(device_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Device not found") from None
    row = (await db.execute(
        text("SELECT id::text AS id, hostname, host(ip_address) AS ip, tags "
             "FROM devices WHERE id = :id"),
        {"id": did},
    )).first()
    if row is None or not scoping.entity_visible(row.tags, await scoping.visible_tags(db, user)):
        raise HTTPException(status_code=404, detail="Device not found")
    return VisibleDevice(id=row.id, hostname=row.hostname, ip=canonical_ip(row.ip),
                         tags=frozenset(tag_set(row.tags)))


async def authorize_mapping_write(
    db: AsyncSession, user: User, vis: FlowVisibility, exporter, device_id=None,
) -> tuple[str, VisibleDevice | None]:
    """Authorize creating, changing or removing an exporter→device mapping.

    A mapping decides who can read an exporter's flows, so it is guarded on
    both ends:

    * ``netflow.manage`` is required (403);
    * a scoped caller may only re-map an exporter it can already see -- an
      out-of-scope or unowned exporter is 404, otherwise a scoped manager
      could pull another team's (or an unclaimed) exporter into their own
      scope by mapping it to one of their devices;
    * the target device must exist and be visible to the caller (404).

    Returns (canonical exporter, target device or None).
    """
    if not vis.can(MANAGE_PERMISSION):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    ip = require_exporter_visible(vis, exporter)
    device = await require_device_visible(db, user, device_id) if device_id is not None else None
    return ip, device


def can_acknowledge_findings(vis: FlowVisibility) -> bool:
    """Acknowledge/assign detection findings: ``alerts.acknowledge`` or
    ``netflow.manage`` (suppression needs ``netflow.manage`` alone)."""
    return vis.can("alerts.acknowledge") or vis.can(MANAGE_PERMISSION)


# ── ClickHouse predicate ────────────────────────────────────────────────────

def exporters_predicate(
    exporters: Iterable[str],
    *,
    v4_col: str = "exporter_ip",
    v6_col: str | None = "exporter_ip6",
    param: str = "exporters",
) -> tuple[str, dict]:
    """(SQL predicate, params): the row comes from one of ``exporters``
    (canonical IP text). ``("0", {})`` for an empty list -- no rows, never
    an unfiltered scan. IPv4 exporters match on the IPv4 column (whose IPv6
    twin is ``::`` for them, contract §3.1); IPv6 exporters need ``v6_col``
    -- pass ``None`` for a table without it, which cannot hold them anyway.
    """
    ips = {canonical_ip(e) for e in exporters} - {None}
    v4 = sorted(ip for ip in ips if not is_ipv6(ip))
    v6 = sorted(ip for ip in ips if is_ipv6(ip))
    terms: list[str] = []
    params: dict = {}
    if v4:
        v4_term = f"{v4_col} IN %({param}_v4)s"
        if v6_col:
            v4_term = f"({v6_col} = toIPv6('::') AND {v4_term})"
        terms.append(v4_term)
        params[f"{param}_v4"] = v4
    if v6 and v6_col:
        terms.append(f"{v6_col} IN %({param}_v6)s")
        params[f"{param}_v6"] = v6
    if not terms:
        return "0", {}
    return ("(" + " OR ".join(terms) + ")", params)


def visibility_predicate(
    vis: FlowVisibility,
    *,
    v4_col: str = "exporter_ip",
    v6_col: str | None = "exporter_ip6",
    param: str = "vis_exporters",
) -> tuple[str, dict]:
    """(SQL predicate, params) restricting rows to the visible exporters.

    ``("", {})`` when unrestricted and ``("0", {})`` when the visible set is
    empty, so a caller can always write ``WHERE ... AND {pred}`` and an empty
    scope yields no rows instead of an unfiltered scan.
    """
    if vis.exporters is None:
        return "", {}
    return exporters_predicate(vis.exporters, v4_col=v4_col, v6_col=v6_col, param=param)


# ── Engine hook: invalidate on ownership-changing commits ───────────────────

_TABLES = r"(?:only\s+)?(?:public\.)?\"?(devices|netflow_exporter_devices)\"?(?![\w.])"
_ROW_SET_WRITE_RE = re.compile(
    rf"\b(?:insert\s+into|delete\s+from|truncate(?:\s+table)?)\s+{_TABLES}", re.IGNORECASE)
_UPDATE_RE = re.compile(rf"\bupdate\s+{_TABLES}", re.IGNORECASE)
# An assignment to a column ownership depends on: ``SET tags = ...`` or
# ``..., ip_address = ...`` (optionally alias-qualified), not a comparison in
# a WHERE clause or a mention in RETURNING.
_OWNERSHIP_COLUMN_SET_RE = re.compile(
    r"(?:\bset\b|,)\s*(?:\"?\w+\"?\.)?\"?(?:tags|ip_address)\"?\s*=(?!=)", re.IGNORECASE)
_DIRTY_KEY = "netflow_visibility_dirty"


def is_ownership_write(statement: str) -> bool:
    """Whether a SQL statement can change exporter ownership or device tags.

    Any row added to or removed from ``devices`` or the mapping table counts,
    as does any update of the mapping table; an update of ``devices`` counts
    only when it assigns ``tags`` or ``ip_address``. Status/last-seen updates
    -- sensors and health sweeps send them continuously -- must not flush
    the cache, or it would never live long enough to help.
    """
    if _ROW_SET_WRITE_RE.search(statement):
        return True
    m = _UPDATE_RE.search(statement)
    if m is None:
        return False
    return m.group(1).lower() == "netflow_exporter_devices" or bool(
        _OWNERSHIP_COLUMN_SET_RE.search(statement, m.end()))


def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    # Cheap substring test first: this runs for every statement the app sends.
    if "devices" not in statement and "DEVICES" not in statement:
        return
    if is_ownership_write(statement):
        conn.info[_DIRTY_KEY] = True


def _on_commit(conn):
    if conn.info.pop(_DIRTY_KEY, False):
        invalidate_visibility_cache()


def _on_rollback(conn):
    conn.info.pop(_DIRTY_KEY, None)


def _install_engine_hooks() -> None:
    if not event.contains(Engine, "before_cursor_execute", _on_execute):
        event.listen(Engine, "before_cursor_execute", _on_execute)
        event.listen(Engine, "commit", _on_commit)
        event.listen(Engine, "rollback", _on_rollback)


_install_engine_hooks()
