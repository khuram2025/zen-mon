"""Readings for combined (multi-condition) rules: devices, service checks, and
the links between assets (docs/multi-condition-rules.md).

Each reader returns ``{subject id: {condition id: rule_conditions.LeafReading}}``
for a batch of subjects, reading each source once. A value that is stale or
cannot be judged is None (unknown); a subject the source cannot read at all
gets ``known=False``. Server readings stay in ``host_alert_service`` (its
history caches), which also serves linked-server conditions.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import rule_conditions as rc

logger = logging.getLogger("zenplus.combined_rules")

# A device reading older than this many poll / ping intervals (and at least
# MIN_FRESH_S) is unknown, as in the device engine.
FRESH_INTERVALS = 3
MIN_FRESH_S = 180
DEVICE_METRICS = set(rc.DEVICE_METRICS)
CHECK_METRICS = set(rc.SERVICE_CHECK_METRICS)


# ── Assets ───────────────────────────────────────────────────────────────────

@dataclass
class Device:
    id: str
    hostname: str
    ip: str
    status: str
    device_type: Optional[str]
    location: Optional[str]
    group_id: Optional[str]
    tags: set
    profile_id: Optional[str]
    groups: Optional[set]
    last_rtt_ms: Optional[float]
    last_seen: Optional[datetime]
    ping_interval: int
    poll_interval: int
    snmp: bool

    def as_scope(self) -> dict:
        """The shape network_alert_service._device_in_scope reads."""
        return {"groups": self.groups, "group_id": self.group_id, "device_type": self.device_type,
                "location": self.location, "tags": self.tags, "profile_id": self.profile_id}


async def load_devices(db: AsyncSession, ids: Optional[Iterable[str]] = None) -> dict[str, Device]:
    """{device id: Device}; all devices, or the ones named."""
    where, params = "", {}
    if ids is not None:
        ids = [str(i) for i in ids if i]
        if not ids:
            return {}
        where, params = "WHERE d.id::text = ANY(CAST(:ids AS text[]))", {"ids": ids}
    rows = (await db.execute(text(
        "SELECT d.id::text, d.hostname, COALESCE(host(d.ip_address), host(d.managed_ip)), d.status, "
        "d.device_type, d.location, d.group_id::text, d.tags, d.profile_id::text, d.last_rtt_ms, d.last_seen, "
        "COALESCE(d.ping_interval, 60), COALESCE(d.snmp_poll_interval, 60), COALESCE(d.snmp_enabled, false) "
        f"FROM devices d {where}"), params)).all()
    from app.services.device_groups import membership
    groups = await membership.load(db, "device")
    out = {}
    for r in rows:
        tags = {str(t).strip().lower() for t in (r[7] or []) if str(t).strip()}
        out[r[0]] = Device(r[0], r[1] or "device", r[2] or "", (r[3] or "unknown").lower(), r[4], r[5], r[6],
                           tags, r[8], (groups.get(r[0], set()) if groups is not None else None),
                           r[9], r[10], int(r[11] or 60), int(r[12] or 60), bool(r[13]))
    return out


async def server_device_links(db: AsyncSession) -> dict[str, str]:
    """{server id: its ping device id}: servers.device_id, else a device with
    the server's address (server_availability's link)."""
    rows = (await db.execute(text(
        """SELECT s.id::text, d.id::text FROM servers s
           JOIN LATERAL (
               SELECT dv.id FROM devices dv
                WHERE dv.id = s.device_id OR (s.primary_ip IS NOT NULL AND dv.ip_address = s.primary_ip)
                ORDER BY (dv.id = s.device_id) DESC NULLS LAST LIMIT 1) d ON TRUE"""))).all()
    return {r[0]: r[1] for r in rows}


async def server_names(db: AsyncSession, ids: Iterable[str]) -> dict[str, str]:
    ids = [str(i) for i in ids if i]
    if not ids:
        return {}
    rows = (await db.execute(text("SELECT id::text, hostname FROM servers WHERE id::text = ANY(CAST(:ids AS text[]))"),
                             {"ids": ids})).all()
    return {r[0]: r[1] or "server" for r in rows}


# ── Device readings ──────────────────────────────────────────────────────────

def _fresh(dev: Device, interval: int) -> float:
    return max(MIN_FRESH_S, FRESH_INTERVALS * max(1, interval))


def _latest_snmp(device_ids: list[str], keys: list[str], since: datetime) -> dict[tuple[str, str], tuple[float, float]]:
    """{(device, metric): (value, epoch s)} for the newest sample of each key."""
    from app.core.database import get_clickhouse_client
    rows = get_clickhouse_client().query(
        """SELECT toString(device_id), metric_key, argMax(value, timestamp),
                  toUnixTimestamp64Milli(max(timestamp)) / 1000
           FROM zenplus.snmp_metrics
           WHERE timestamp >= %(since)s AND metric_key IN %(keys)s
             AND toString(device_id) IN %(ids)s
           GROUP BY device_id, metric_key""",
        parameters={"since": since, "keys": keys, "ids": device_ids}).result_rows
    return {(str(r[0]), str(r[1])): (float(r[2]) if r[2] is not None else math.nan, float(r[3])) for r in rows}


async def device_readings(db: AsyncSession, devices: dict[str, Device], conditions: list[dict], *,
                          now: float, unknown: set[str] = frozenset()) -> dict[str, dict[str, rc.LeafReading]]:
    """Readings of device conditions for each device. ``unknown`` devices (in
    maintenance, say) read as unknown. Raises when a source cannot be read."""
    import asyncio
    out: dict[str, dict[str, rc.LeafReading]] = {did: {} for did in devices}
    if not devices or not conditions:
        return out
    keys = sorted({c["metric"] for c in conditions} & {"cpu", "memory"})
    snmp = {}
    if keys:
        since = datetime.fromtimestamp(now - 3 * 3600, timezone.utc)
        snmp = await asyncio.to_thread(_latest_snmp, list(devices), keys, since)
    poll = {}
    if any(c["metric"] == "snmp_unreachable" for c in conditions):
        rows = (await db.execute(text(
            "SELECT device_id::text, failing_since, last_attempt_at FROM device_snmp_poll_state"
            " WHERE device_id::text = ANY(CAST(:ids AS text[]))"), {"ids": list(devices)})).all()
        poll = {r[0]: (r[1], r[2]) for r in rows}
    for did, dev in devices.items():
        for c in conditions:
            cid, metric = c["id"], c["metric"]
            if did in unknown or dev.status == "maintenance":
                out[did][cid] = rc.LeafReading(known=False, asset=dev.hostname)
                continue
            value: Optional[float] = None
            if metric in ("device_down", "device_degraded"):
                if dev.status in ("up", "down", "degraded"):
                    value = 1.0 if dev.status == metric.removeprefix("device_") else 0.0
            elif metric == "rtt":
                seen = dev.last_seen.timestamp() if dev.last_seen else None
                if dev.status != "down" and dev.last_rtt_ms is not None and seen is not None \
                        and now - seen < _fresh(dev, dev.ping_interval):
                    value = float(dev.last_rtt_ms)
            elif metric in ("cpu", "memory"):
                sample = snmp.get((did, metric))
                if sample and math.isfinite(sample[0]) and now - sample[1] < _fresh(dev, dev.poll_interval):
                    value = sample[0]
            elif metric == "snmp_unreachable":
                state = poll.get(did)
                if state is None or not dev.snmp:
                    out[did][cid] = rc.LeafReading(known=False, asset=dev.hostname)
                    continue
                failing_since, attempted = state
                if dev.status == "up" and attempted is not None:
                    value = 0.0 if failing_since is None else max(0.0, (now - failing_since.timestamp()) / 60.0)
            out[did][cid] = rc.LeafReading({"": value}, asset=dev.hostname)
    return out


# ── Service check readings ───────────────────────────────────────────────────

@dataclass
class Check:
    id: str
    name: str
    status: str
    enabled: bool
    stale: bool
    device_id: Optional[str]
    server_id: Optional[str]
    group_id: Optional[str]
    tags: list
    last_response_ms: Optional[float]
    tls_days_remaining: Optional[float]
    target: str


async def load_checks(db: AsyncSession, ids: Optional[Iterable[str]] = None) -> dict[str, Check]:
    from types import SimpleNamespace
    from app.services.service_check_service import evidence_freshness
    where, params = "", {}
    if ids is not None:
        ids = [str(i) for i in ids if i]
        if not ids:
            return {}
        where, params = "WHERE id::text = ANY(CAST(:ids AS text[]))", {"ids": ids}
    rows = (await db.execute(text(
        "SELECT id::text, name, status, enabled, last_check_at, check_interval, down_check_interval, timeout, "
        "device_id::text, server_id::text, group_id::text, tags, last_response_ms, tls_days_remaining, "
        f"COALESCE(target_url, target_host) FROM service_checks {where}"), params)).all()
    now = datetime.now(timezone.utc)
    out = {}
    for r in rows:
        probe = SimpleNamespace(last_check_at=r[4], enabled=r[3], status=r[2], check_interval=r[5],
                                down_check_interval=r[6], timeout=r[7])
        stale, _age = evidence_freshness(probe, now)
        out[r[0]] = Check(r[0], r[1] or "service check", (r[2] or "unknown").lower(), bool(r[3]), stale,
                          r[8], r[9], r[10], list(r[11] or []), r[12], r[13], r[14] or "")
    return out


async def check_readings(checks: dict[str, Check], conditions: list[dict], *,
                         unknown: set[str] = frozenset()) -> dict[str, dict[str, rc.LeafReading]]:
    """Readings of service-check conditions. A paused, stale or never-run
    check reads as unknown."""
    out: dict[str, dict[str, rc.LeafReading]] = {cid: {} for cid in checks}
    for chk_id, chk in checks.items():
        for c in conditions:
            cid, metric = c["id"], c["metric"]
            if chk_id in unknown or not chk.enabled or chk.stale or chk.status == "unknown":
                out[chk_id][cid] = rc.LeafReading(known=False, asset=chk.name)
                continue
            value: Optional[float] = None
            if metric == "service_down":
                value = 1.0 if chk.status == "down" else 0.0
            elif metric == "service_degraded":
                value = 1.0 if chk.status in ("degraded", "warning") else 0.0
            elif metric == "service_response_ms":
                value = None if chk.status == "down" or chk.last_response_ms is None else float(chk.last_response_ms)
            elif metric == "service_tls_days":
                value = None if chk.tls_days_remaining is None else float(chk.tls_days_remaining)
            out[chk_id][cid] = rc.LeafReading({"": value}, asset=chk.name)
    return out


# ── Linked conditions ────────────────────────────────────────────────────────

def split(tree: dict) -> dict[str, list[dict]]:
    """The tree's conditions by what they read: {"self": [...], "device": [...], "server": [...]}."""
    out: dict[str, list[dict]] = {}
    for c in rc.leaves(tree):
        out.setdefault(c.get("on") or "self", []).append(c)
    return out


def relink(per_subject: dict[str, str | None], linked: dict[str, dict[str, rc.LeafReading]],
           conditions: list[dict], names: dict[str, str]) -> dict[str, dict[str, rc.LeafReading]]:
    """Readings of linked conditions per subject: the linked asset's readings,
    or unknown when the subject has no linked asset (or it reads nothing)."""
    out: dict[str, dict[str, rc.LeafReading]] = {}
    for subject, other in per_subject.items():
        got = linked.get(other or "", {})
        out[subject] = {}
        for c in conditions:
            r = got.get(c["id"])
            if r is None:
                r = rc.LeafReading(known=False)
            if not r.asset and other:
                r = rc.LeafReading(r.values, r.known, r.details, names.get(other, ""))
            out[subject][c["id"]] = r
    return out


# ── Hold state (combined_rule_state, migrate-212) ────────────────────────────

@dataclass
class HoldState:
    breach_since: Optional[datetime] = None
    clear_since: Optional[datetime] = None
    leaf_since: dict = None

    def __post_init__(self):
        self.leaf_since = dict(self.leaf_since or {})


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def load_states(db: AsyncSession, rule_id) -> dict[str, HoldState]:
    import json
    rows = (await db.execute(text(
        "SELECT subject_key, breach_since, clear_since, leaf_since FROM combined_rule_state WHERE rule_id = :rid"),
        {"rid": str(rule_id)})).all()
    out = {}
    for key, b, c, leaf in rows:
        if isinstance(leaf, str):
            leaf = json.loads(leaf or "{}")
        out[key] = HoldState(_aware(b), _aware(c), leaf or {})
    return out


async def save_state(db: AsyncSession, rule_id, key: str, state: HoldState, at: datetime) -> None:
    import json
    await db.execute(text(
        "INSERT INTO combined_rule_state (rule_id, subject_key, breach_since, clear_since, leaf_since, updated_at) "
        "VALUES (:rid, :k, :b, :c, CAST(:l AS jsonb), :at) "
        "ON CONFLICT (rule_id, subject_key) DO UPDATE SET breach_since = EXCLUDED.breach_since, "
        "clear_since = EXCLUDED.clear_since, leaf_since = EXCLUDED.leaf_since, updated_at = EXCLUDED.updated_at"),
        {"rid": str(rule_id), "k": key, "b": state.breach_since, "c": state.clear_since,
         "l": json.dumps(state.leaf_since or {}), "at": at})
