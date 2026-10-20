"""License node capacity: what one license node is and when adding one is allowed.

A node is one monitored thing: a network device or a server. A server linked to
a device (``servers.device_id``, as discovery's "both" import and Add Server with
a device do) is the same machine seen twice and counts once. Controller-reported
children (``poll_mode = 'via_controller'``, a WLC's thin APs) never consume a
node. Service checks are not nodes.

The cap comes from the zentryc.com subscription snapshot the updater and the
dashboard cache in ``subscription.json`` on every check-in:

* ``license.total_node_cap`` is the subscription's node pool, shared by every
  appliance on it; ``used_node_count`` is the pool's usage as of the snapshot.
  This appliance may use the pool minus what the *other* appliances used, i.e.
  ``total - (used - reported_node_count)``, where ``reported_node_count`` is the
  count this appliance sent with that check-in. Snapshots written before that
  field existed are treated as this appliance owning the whole pool.
* No node cap in the snapshot falls back to ``max_devices``; neither means the
  license server does not meter nodes and nothing is enforced.
* An expired or inactive subscription blocks additions.
* An unregistered appliance gets the evaluation allowance.

Enforcement only ever refuses *new* nodes. Existing devices and servers keep
being monitored when a license shrinks, lapses or is exceeded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SUBSCRIPTION_PATH = Path("/opt/zenplus/updater/config/subscription.json")

# Unregistered appliances (labs, evaluations) — matches the local trial plan.
EVALUATION_NODE_LIMIT = 50
# Usage at or above this share of the limit shows the "nearly full" warning.
NEAR_LIMIT_RATIO = 0.9

ERROR_CODE = "license_capacity_reached"

# Kept textually identical to updater/inventory.py NODE_COUNT_SQL (asserted by
# test_license_capacity): the count the appliance reports to zentryc.com must
# be the count it enforces.
NODE_COUNT_SQL = """
SELECT
  (SELECT COUNT(*) FROM devices d
    WHERE d.poll_mode IS DISTINCT FROM 'via_controller') AS devices,
  (SELECT COUNT(*) FROM servers s
    WHERE s.device_id IS NULL
       OR NOT EXISTS (SELECT 1 FROM devices d
                       WHERE d.id = s.device_id
                         AND d.poll_mode IS DISTINCT FROM 'via_controller')) AS servers,
  (SELECT COUNT(*) FROM servers s
    WHERE EXISTS (SELECT 1 FROM devices d
                   WHERE d.id = s.device_id
                     AND d.poll_mode IS DISTINCT FROM 'via_controller')) AS linked_servers,
  (SELECT COUNT(*) FROM devices d
    WHERE d.poll_mode = 'via_controller') AS controller_children
"""

_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('license-node-capacity', 0))"


@dataclass(frozen=True)
class NodeUsage:
    devices: int = 0          # directly monitored devices (each one node)
    servers: int = 0          # servers not linked to a counted device (each one node)
    linked_servers: int = 0   # servers sharing their linked device's node
    controller_children: int = 0  # via-controller children, never counted

    @property
    def nodes(self) -> int:
        return self.devices + self.servers


@dataclass(frozen=True)
class LicenseCapacity:
    # ok | near_limit | full | over_limit | expired | inactive | unmetered
    state: str
    source: str               # license | evaluation | unmetered
    enforced: bool
    can_add: bool
    limit: Optional[int]      # nodes this appliance may use; None = no limit
    used: int
    available: Optional[int]  # None = no limit
    usage: NodeUsage
    pool_total: Optional[int] = None         # subscription-wide node cap
    pool_other_usage: Optional[int] = None   # nodes the other appliances use
    plan: Optional[str] = None
    expires_at: Optional[str] = None
    snapshot_at: Optional[str] = None
    message: str = ""

    @property
    def pool_used(self) -> Optional[int]:
        """Subscription-wide usage as of now: the other appliances' share from
        the last check-in plus this appliance's live count. zentryc.com's own
        ``used_node_count`` only moves on a check-in."""
        if self.pool_total is None:
            return None
        return (self.pool_other_usage or 0) + self.used

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["usage"] = {**asdict(self.usage), "nodes": self.usage.nodes}
        out["pool_used"] = self.pool_used
        out["pool_available"] = (None if self.pool_total is None
                                 else max(0, self.pool_total - self.pool_used))
        return out


def load_snapshot(path: Path | None = None) -> Optional[dict]:
    """The cached zentryc.com subscription, or None when unregistered/unreadable."""
    p = path or SUBSCRIPTION_PATH
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        logger.warning("license snapshot %s is unreadable", p)
        return None
    if not isinstance(data, dict) or not data:
        return None
    try:
        data.setdefault("_snapshot_at", datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat())
    except OSError:
        pass
    return data


def annotate_snapshot(subscription: dict, reported_node_count: int) -> dict:
    """Copy of a check-in subscription carrying the node count sent with it.

    A count of 0 is also what a failed count query reports, so it is not
    recorded: without the field the whole pool is treated as this appliance's,
    rather than a failed query handing every node to the other appliances.
    """
    if int(reported_node_count or 0) <= 0:
        return dict(subscription)
    return {**subscription, "reported_node_count": int(reported_node_count)}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _extend_hint(source: str) -> str:
    if source == "evaluation":
        return "Register this appliance with a license key in Settings > General > Licenses to add more."
    return "Extend your license to add more devices or servers."


def compute_capacity(
    snapshot: Optional[dict],
    usage: NodeUsage,
    *,
    now: Optional[datetime] = None,
) -> LicenseCapacity:
    """Pure decision from a subscription snapshot and the local usage."""
    now = now or datetime.now(timezone.utc)
    used = usage.nodes
    common = {"usage": usage, "used": used}

    if not snapshot:
        limit = EVALUATION_NODE_LIMIT
        source = "evaluation"
        pool_total = other = None
        plan = expires_at = snapshot_at = None
    else:
        plan = snapshot.get("name") or snapshot.get("plan") or None
        expires_at = snapshot.get("expires_at")
        snapshot_at = snapshot.get("_snapshot_at")
        expiry = _parse_ts(expires_at)
        expired = bool(snapshot.get("is_expired")) or (expiry is not None and expiry <= now)
        inactive = snapshot.get("is_active") is False
        meta = {"plan": plan, "expires_at": expires_at, "snapshot_at": snapshot_at}
        lic = snapshot.get("license") if isinstance(snapshot.get("license"), dict) else {}
        pool_total = _int(lic.get("total_node_cap"))
        pool_used = _int(lic.get("used_node_count"))
        if not pool_total or pool_total <= 0:
            pool_total = _int(snapshot.get("max_devices"))
            pool_used = None
        if not pool_total or pool_total <= 0:
            pool_total = None
        reported = _int(snapshot.get("reported_node_count"))
        other = 0
        if pool_used is not None and reported is not None:
            other = max(0, pool_used - reported)
        pool = {"pool_total": pool_total, "pool_other_usage": other if pool_total else None}
        if expired or inactive:
            state = "expired" if expired else "inactive"
            word = "expired" if expired else "is not active"
            return LicenseCapacity(
                state=state, source="license", enforced=True, can_add=False,
                limit=None, available=0, **common, **meta, **pool,
                message=(f"Your license {word}. Existing devices and servers are still "
                         "monitored, but no new ones can be added until the license is renewed."),
            )
        if pool_total is None:
            return LicenseCapacity(
                state="unmetered", source="unmetered", enforced=False, can_add=True,
                limit=None, available=None, **common, **meta,
                message="The license server does not set a node limit for this appliance.",
            )
        limit = max(0, pool_total - other)
        source = "license"

    available = max(0, limit - used)
    hint = _extend_hint(source)
    if used > limit:
        state = "over_limit"
        message = (f"This appliance uses {used} of {limit} licensed nodes, more than the license "
                   f"allows. You have reached your license capacity. {hint}")
    elif used == limit:
        state = "full"
        message = f"You have reached your license capacity ({used} of {limit} nodes). {hint}"
    elif used >= limit * NEAR_LIMIT_RATIO:
        state = "near_limit"
        message = f"{available} of {limit} licensed nodes left."
    else:
        state = "ok"
        message = f"{available} of {limit} licensed nodes available."
    return LicenseCapacity(
        state=state, source=source, enforced=True, can_add=available > 0,
        limit=limit, available=available, pool_total=pool_total,
        pool_other_usage=other, plan=plan, expires_at=expires_at,
        snapshot_at=snapshot_at, message=message, **common,
    )


async def count_usage(db: AsyncSession) -> NodeUsage:
    row = (await db.execute(text(NODE_COUNT_SQL))).mappings().first()
    if not row:
        return NodeUsage()
    return NodeUsage(**{k: int(row[k] or 0) for k in
                        ("devices", "servers", "linked_servers", "controller_children")})


async def count_nodes(db: AsyncSession) -> int:
    return (await count_usage(db)).nodes


async def get_capacity(db: AsyncSession, *, snapshot: Optional[dict] = None) -> LicenseCapacity:
    return compute_capacity(snapshot if snapshot is not None else load_snapshot(), await count_usage(db))


def capacity_error(cap: LicenseCapacity, *, requested: int = 1) -> HTTPException:
    """402 with a machine-readable code; apiErrorMessage shows ``message``."""
    message = cap.message
    if cap.can_add and cap.available is not None and requested > cap.available:
        message = (f"Adding {requested} nodes would exceed your license capacity: only "
                   f"{cap.available} of {cap.limit} nodes are left. {_extend_hint(cap.source)}")
    return HTTPException(status_code=402, detail={
        "code": ERROR_CODE,
        "message": message,
        "capacity": cap.as_dict(),
    })


class NodeBudget:
    """Remaining nodes inside one transaction, for single and bulk creates.

    ``reserve`` takes a transaction-scoped advisory lock first, so two requests
    racing for the last node serialize; the lock is released on commit or
    rollback. Callers commit the rows they created in the same transaction.
    """

    def __init__(self, cap: LicenseCapacity):
        self.capacity = cap
        self.remaining: Optional[int] = None if not cap.enforced else (cap.available or 0)
        if cap.enforced and not cap.can_add:
            self.remaining = 0

    @property
    def unlimited(self) -> bool:
        return self.remaining is None

    def take(self, n: int = 1) -> bool:
        if n <= 0 or self.remaining is None:
            return True
        if self.remaining < n:
            return False
        self.remaining -= n
        return True

    def require(self, n: int = 1) -> None:
        if not self.take(n):
            raise capacity_error(self.capacity, requested=n)

    @property
    def exhausted_message(self) -> str:
        return self.capacity.message if not self.capacity.can_add else (
            f"You have reached your license capacity ({self.capacity.limit} nodes). "
            f"{_extend_hint(self.capacity.source)}")


async def reserve(db: AsyncSession, *, snapshot: Optional[dict] = None) -> NodeBudget:
    await db.execute(text(_LOCK_SQL))
    return NodeBudget(await get_capacity(db, snapshot=snapshot))


async def ensure_can_add(db: AsyncSession, nodes: int = 1) -> NodeBudget:
    """Raise the capacity error unless ``nodes`` new nodes fit; lock held to commit."""
    budget = await reserve(db)
    budget.require(nodes)
    return budget


async def server_needs_node(db: AsyncSession, device_id: Any) -> bool:
    """Whether a new server linked to ``device_id`` consumes a node of its own."""
    if not device_id:
        return True
    row = (await db.execute(
        text("""SELECT 1 FROM devices
                WHERE id = CAST(:id AS uuid) AND poll_mode IS DISTINCT FROM 'via_controller'"""),
        {"id": str(device_id)},
    )).first()
    return row is None
