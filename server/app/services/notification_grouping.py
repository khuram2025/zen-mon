"""Warning grouping and per-recipient notification budgets (NOTIFY 02).

The October 2026 field review counted 172 warning mails in 48 hours, most of
them separate messages about related conditions. This module lets an operator
trade a short wait for one summary, without ever delaying what matters:

* Only TRIGGER notifications of P3/P4 (warning/info) incidents whose caller
  marked them ``groupable`` can be held. P1/P2 or critical-severity
  notifications, recoveries, escalations, reviews and reminders always go out
  at once.
* Grouping: a holdable trigger joins the open digest of its group (group key
  from ``group_by``: appliance, site, device_group, scope_tag, event_family)
  for the channel and recipient set it was going to. The digest is released
  ``group_wait_seconds`` after its first item; a group that was summarised
  less than ``group_interval_seconds`` ago waits until that interval has
  passed (Alertmanager semantics). Reaching ``max_items`` releases it at the
  next sweep.
* Budget: when a recipient already received ``budget_per_recipient_per_hour``
  non-critical notifications in the last hour, further holdable triggers to
  them go to a per-recipient "budget" digest instead (released every group
  interval). Nothing is dropped. With ``critical_reserve`` critical (P1/P2)
  traffic is not counted against the budget; it is never held either way.
* A digest is ONE summary per channel and recipient set (email, SMS or
  webhook) listing every child incident with its reference, asset,
  condition, value, first-seen time and link. Items whose incident resolved
  before release are listed as "already recovered"; when every item resolved
  the digest is cancelled with that reason and nothing is sent. A recovery
  for an incident still waiting in an open digest is folded into that digest
  instead of being mailed on its own (the recipient never heard the trigger).
* Every held message records a timeline event (``notification_held``) with
  its reason and release condition, and a delivery-log entry ``held``.

The digest is sent through ``host_alert_service.dispatch_to_channels`` with
the durable delivery key event ``digest:<id>`` (anchored on the digest's first
incident), so a crash between claim and send never mails a summary twice; the
claim itself is a status transition open -> sending under ``FOR UPDATE SKIP
LOCKED``, so two sweeps never both send one digest.

Settings live in ``system_settings['notification_grouping']`` and are OFF by
default (an upgrade changes nothing until an operator enables them).
Schema: scripts/migrate-164-escalation-matrix-grouping.sql.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_priority as prio

logger = logging.getLogger("zenplus.notification_grouping")

SETTINGS_KEY = "notification_grouping"
GROUP_BY_CHOICES: tuple[str, ...] = ("appliance", "site", "device_group", "scope_tag", "event_family")
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "group_by": ["site", "event_family"],
    "group_wait_seconds": 60,
    "group_interval_seconds": 300,
    "max_items": 50,
    "budget_per_recipient_per_hour": 30,
    "critical_reserve": True,
}
# "Enable recommended" in the UI: the defaults, switched on.
RECOMMENDED_SETTINGS: dict[str, Any] = {**DEFAULT_SETTINGS, "enabled": True}
LIMITS = {
    "group_wait_seconds": (0, 3600),
    "group_interval_seconds": (30, 86400),
    "max_items": (5, 500),
    "budget_per_recipient_per_hour": (0, 10000),   # 0 = no budget
}

# Channel id used for keys, logs and digests of direct-to-person deliveries
# (escalation targets resolved to a user's email/SMS contact).
DIRECT_CHANNEL_ID = "00000000-0000-0000-0000-0000000000e1"
DIRECT_CHANNEL_NAME = "Direct to person"

REASON_GROUPING = "grouping"
REASON_BUDGET = "budget"
BUDGET_GROUP_KEY = "budget"
RELEASE_BATCH = 50
# A digest claimed (sending) this long ago without finishing was interrupted.
STALE_SENDING_MINUTES = 10

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

EVENT_LABELS = {
    "notification_held": "Notification held for a summary",
    "notification_merged": "Recovery listed in the pending summary",
    "notification_released": "Summary notification sent",
    "notification_release_failed": "Summary notification failed",
    "notification_cancelled": "Summary cancelled (recovered before release)",
}


# ── Settings ─────────────────────────────────────────────────────────────────

def _int_in(v, lo: int, hi: int, default: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, int(v)))


def clean_settings(raw: Any) -> dict[str, Any]:
    """Normalize stored or posted settings; unknown keys are dropped and
    out-of-range numbers clamped to their limits."""
    out = dict(DEFAULT_SETTINGS)
    out["group_by"] = list(DEFAULT_SETTINGS["group_by"])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "critical_reserve" in raw:
        out["critical_reserve"] = bool(raw["critical_reserve"])
    if isinstance(raw.get("group_by"), (list, tuple)):
        out["group_by"] = [g for g in GROUP_BY_CHOICES if g in {str(x) for x in raw["group_by"]}]
    for key, (lo, hi) in LIMITS.items():
        if key in raw:
            v = raw[key]
            if key == "budget_per_recipient_per_hour" and v is None:
                out[key] = 0
            else:
                out[key] = _int_in(v, lo, hi, DEFAULT_SETTINGS[key])
    return out


async def load_settings(db: AsyncSession) -> dict[str, Any]:
    try:
        async with db.begin_nested():
            row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                    {"k": SETTINGS_KEY})).first()
    except Exception as exc:
        logger.warning("grouping settings unreadable, grouping off: %s", exc)
        return clean_settings(None)
    return clean_settings(row[0] if row else None)


async def save_settings(db: AsyncSession, body: Any, actor_id=None) -> dict[str, Any]:
    clean = clean_settings(body)
    await db.execute(text(
        "INSERT INTO system_settings (key, value, updated_at, updated_by)"
        " VALUES (:k, CAST(:v AS jsonb), now(), CAST(:by AS uuid))"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),"
        " updated_by = EXCLUDED.updated_by"
    ), {"k": SETTINGS_KEY, "v": json.dumps(clean), "by": str(actor_id) if actor_id else None})
    return clean


_TABLES_OK: bool | None = None


async def available(db: AsyncSession) -> bool:
    """Whether migration 164's digest tables exist (cached once positive)."""
    global _TABLES_OK
    if _TABLES_OK:
        return True
    try:
        async with db.begin_nested():
            ok = (await db.execute(text(
                "SELECT to_regclass('public.notification_digests') IS NOT NULL"
                " AND to_regclass('public.notification_digest_items') IS NOT NULL"))).scalar()
    except Exception:
        ok = False
    _TABLES_OK = bool(ok)
    return _TABLES_OK


def reset_for_tests() -> None:
    global _TABLES_OK
    _TABLES_OK = None


# ── Event families ───────────────────────────────────────────────────────────

FAMILY_LABELS = {
    "cpu": "CPU", "memory": "Memory", "storage": "Storage", "interface": "Interfaces",
    "reachability": "Reachability", "latency": "Latency and loss", "service": "Service checks",
    "hardware": "Hardware", "reboot": "Reboots", "network": "Network protocols",
    "process": "Processes and services", "apm": "Application performance",
    "endpoint": "Endpoints", "netpath": "Network paths", "netflow": "Traffic",
    "trap": "SNMP traps", "syslog": "Syslog", "template": "Template metrics", "other": "Other",
}

_FAMILY_EXACT = {
    "cpu": "cpu", "host_cpu_pct": "cpu",
    "memory": "memory", "host_memory_pct": "memory",
    "host_filesystem_pct": "storage", "host_disk_util_pct": "storage",
    "ping_status": "reachability", "device_status": "reachability",
    "rtt": "latency", "packet_loss": "latency", "jitter": "latency",
    "service_status": "service",
    "temperature": "hardware", "fan_state": "hardware", "psu_state": "hardware",
    "uptime_reset": "reboot",
    "session_count": "network", "vpn_tunnel_state": "network", "ha_state": "network",
    "bgp_neighbor_down": "network",
    "host_service_down": "process", "host_process_down": "process",
    "trap": "trap", "syslog": "syslog",
}
_FAMILY_PREFIX = (("if_", "interface"), ("apm_", "apm"), ("udt_", "endpoint"),
                  ("netpath_", "netpath"), ("netflow_", "netflow"), ("tpl_", "template"))


def event_family(metric: str | None, *, device: bool = False, server: bool = False,
                 service: bool = False) -> str:
    """The family a rule metric belongs to ("memory", "interface", ...).
    Without a metric the alert's source decides (a device status alert is
    reachability, a service check alert is a service check)."""
    m = str(metric or "").strip().lower()
    if m in _FAMILY_EXACT:
        return _FAMILY_EXACT[m]
    for prefix, fam in _FAMILY_PREFIX:
        if m.startswith(prefix):
            return fam
    if m:
        return "other"
    if service:
        return "service"
    if device:
        return "reachability"
    if server:
        return "other"
    return "other"


# ── The gate inside dispatch_to_channels ─────────────────────────────────────

_INFO_SQL = f"""
SELECT a.id, a.status, a.severity, a.priority, a.message, a.triggered_at,
       a.device_id, a.server_id, a.service_check_id,
       COALESCE(r.metric, a.metadata->>'metric') AS metric, r.name AS rule_name,
       r.priority AS rule_priority,
       COALESCE(d.hostname, sv.display_name, sv.hostname, sc.name) AS asset,
       COALESCE(sv.site_id, sen.site_id) AS site_id, si.name AS site_name,
       COALESCE(d.location, scd.location) AS location,
       COALESCE(d.group_id, scd.group_id) AS group_id, dg.name AS group_name,
       COALESCE(d.tags, sv.tags, scd.tags, to_jsonb(sc.tags)) AS tags,
       sen.id AS sensor_id, sen.name AS sensor_name
FROM alerts a
LEFT JOIN alert_rules r ON r.id = COALESCE(a.rule_id, CASE
     WHEN (a.metadata->>'rule_id') ~ '{_UUID_RE}' THEN (a.metadata->>'rule_id')::uuid END)
LEFT JOIN devices d ON d.id = a.device_id
LEFT JOIN servers sv ON sv.id = a.server_id
LEFT JOIN service_checks sc ON sc.id = a.service_check_id
LEFT JOIN devices scd ON scd.id = sc.device_id
LEFT JOIN sensors sen ON sen.id = COALESCE(a.sensor_id, d.default_sensor_id,
                                           sc.default_sensor_id, scd.default_sensor_id)
LEFT JOIN sites si ON si.id = COALESCE(sv.site_id, sen.site_id)
LEFT JOIN device_groups dg ON dg.id = COALESCE(d.group_id, scd.group_id)
WHERE a.id = CAST(:id AS uuid)
"""


def _tags(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(t).strip().lower() for t in value if str(t or "").strip()})


def group_for(info, dims: list[str], family: str) -> tuple[str, str]:
    """(group key, human label) for an alert under the configured dimensions."""
    keys, labels = [], []
    for dim in dims:
        if dim == "appliance":
            if info.sensor_id:
                keys.append(f"appliance:{info.sensor_id}")
                labels.append(f"Sensor {info.sensor_name or str(info.sensor_id)[:8]}")
            else:
                keys.append("appliance:controller")
                labels.append("Controller")
        elif dim == "site":
            if info.site_id:
                keys.append(f"site:{info.site_id}")
                labels.append(info.site_name or "Site")
            elif (info.location or "").strip():
                loc = info.location.strip()
                keys.append(f"site:location:{loc.lower()}")
                labels.append(loc)
            else:
                keys.append("site:none")
                labels.append("No site")
        elif dim == "device_group":
            if info.group_id:
                keys.append(f"device_group:{info.group_id}")
                labels.append(info.group_name or "Device group")
            else:
                keys.append("device_group:none")
                labels.append("No device group")
        elif dim == "scope_tag":
            tags = _tags(info.tags)
            keys.append("scope_tag:" + (",".join(tags) if tags else "none"))
            labels.append(("Tags " + ", ".join(tags)) if tags else "Untagged")
        elif dim == "event_family":
            keys.append(f"event_family:{family}")
            labels.append(FAMILY_LABELS.get(family, family.title()))
    if not keys:
        return "all", "All warnings"
    return "|".join(keys), " · ".join(labels)


def _first_detail(details, *labels) -> str:
    wanted = {l.lower() for l in labels}
    for pair in details or []:
        try:
            label, value = pair
        except (TypeError, ValueError):
            continue
        if str(label).strip().lower() in wanted and value not in (None, ""):
            return str(value)
    return ""


def item_summary(ctx: dict, info, priority: str) -> dict:
    """What the digest lists for one held notification."""
    from app.services.alert_phrasing import incident_ref

    metric = ctx.get("headline_metric") or {}
    value = (metric.get("value") if isinstance(metric, dict) else None) or _first_detail(
        ctx.get("details"), "Measured", "Current value", "Value", "Reading") or ""
    condition = (ctx.get("condition_label") or _first_detail(ctx.get("details"), "Condition")
                 or ctx.get("rule_name") or info.rule_name or info.message or "")
    first_seen = ctx.get("first_seen") or (info.triggered_at.isoformat() if info.triggered_at else "")
    return {
        "incident_ref": ctx.get("incident_ref") or incident_ref(info.id),
        "asset": ctx.get("hostname") or info.asset or "",
        "condition": str(condition)[:300],
        "value": str(value)[:120],
        "first_seen": str(first_seen),
        "path": f"/alerts/{info.id}",
        "severity": (ctx.get("severity") or info.severity or "warning").lower(),
        "priority": priority,
        "rule_name": ctx.get("rule_name") or info.rule_name or "",
        "subject": str(ctx.get("subject") or "")[:300],
        "message": str(ctx.get("message") or info.message or "")[:400],
    }


@dataclass
class Routed:
    """One hold (or recovery fold) the gate made for part of a channel."""
    action: str                  # "held" | "merged" | "duplicate"
    keys: list[str]
    digest_id: str | None = None
    reason: str = ""
    release_condition: str = ""

    @property
    def log_outcome(self) -> str:
        return "held" if self.action == "held" else "skipped"

    @property
    def log_text(self) -> str:
        ref = f"digest {self.digest_id[:8]}" if self.digest_id else "a digest"
        if self.action == "held":
            return f"held: {self.reason} — {self.release_condition}"[:500]
        if self.action == "merged":
            return (f"held: recovered before {ref} was released; the summary lists it as "
                    "already recovered")
        return f"duplicate: already held for this event in {ref}"


def _fmt_utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M:%S UTC")


def _direct_recipient(kind: str, key: str) -> str:
    return f"{kind}:{key}"


@dataclass
class Gate:
    """Decides, per channel and recipient, whether a send goes out now, is
    held into a digest, or (for a recovery) is folded into a pending digest."""
    mode: str                                    # "hold" | "fold"
    alert_id: str
    event: str
    settings: dict = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)
    info: Any = None
    priority: str = "P3"
    group_key: str = ""
    group_label: str = ""
    family: str = "other"
    fold_digests: list = field(default_factory=list)
    changed: bool = False

    @property
    def grouping_active(self) -> bool:
        return self.mode == "hold" and int(self.settings.get("group_wait_seconds") or 0) > 0

    @property
    def budget(self) -> int:
        return int(self.settings.get("budget_per_recipient_per_hour") or 0) if self.mode == "hold" else 0

    async def route(self, db: AsyncSession, *, channel_id: str, channel_name: str | None,
                    channel_type: str, targets: list[tuple[str, str]],
                    direct_kind: str | None = None) -> tuple[list[tuple[str, str]], list[Routed]]:
        """(targets to send now, holds made). ``targets`` are (dedupe key,
        address); a webhook-style channel passes [("", "")]."""
        if not targets:
            return targets, []
        try:
            async with db.begin_nested():
                result = await self._route(db, channel_id=channel_id, channel_name=channel_name,
                                           channel_type=channel_type, targets=targets,
                                           direct_kind=direct_kind)
        except Exception as exc:
            # Never lose a notification to the gate: send it as before.
            logger.warning("grouping: hold failed for alert %s on channel %s, sending now: %s",
                           self.alert_id, channel_id, exc)
            return targets, []
        if result[1]:
            await db.commit()
        return result

    async def _route(self, db: AsyncSession, *, channel_id: str, channel_name: str | None,
                     channel_type: str, targets: list[tuple[str, str]],
                     direct_kind: str | None = None) -> tuple[list[tuple[str, str]], list[Routed]]:
        if self.mode == "fold":
            return await self._fold(db, channel_id, targets, direct_kind)
        if self.grouping_active:
            if direct_kind:
                out = []
                for key, _addr in targets:
                    out.append(await self._hold(db, channel_id=channel_id, channel_name=channel_name,
                                                recipient=_direct_recipient(direct_kind, key),
                                                keys=[key], reason=REASON_GROUPING))
                return [], out
            recipient = ",".join(sorted(k for k, _ in targets if k))
            return [], [await self._hold(db, channel_id=channel_id, channel_name=channel_name,
                                         recipient=recipient, keys=[k for k, _ in targets],
                                         reason=REASON_GROUPING)]
        if self.budget:
            over = await self._over_budget(db, channel_id, [k for k, _ in targets])
            if not over:
                return targets, []
            out, keep = [], []
            for key, addr in targets:
                if key not in over:
                    keep.append((key, addr))
                    continue
                recipient = _direct_recipient(direct_kind, key) if direct_kind else key
                out.append(await self._hold(db, channel_id=channel_id, channel_name=channel_name,
                                            recipient=recipient, keys=[key], reason=REASON_BUDGET,
                                            sent_last_hour=over[key]))
            return keep, out
        return targets, []

    # -- recovery folding ----------------------------------------------------

    async def _fold(self, db, channel_id, targets, direct_kind):
        keep, out = list(targets), []
        for dg in self.fold_digests:
            if str(dg.channel_id or "") != str(channel_id):
                continue
            if direct_kind:
                members = {dg.recipient}
                hit = [(k, a) for k, a in keep if _direct_recipient(direct_kind, k) in members]
            elif dg.recipient:
                members = set(dg.recipient.split(","))
                hit = [(k, a) for k, a in keep if k in members]
            else:
                hit = list(keep)
            if not hit:
                continue
            keep = [t for t in keep if t not in hit]
            from app.services.alert_workflow import record_event
            await record_event(db, self.alert_id, "notification_merged", source="system",
                               reason="Recovered before the pending summary was released",
                               detail={"digest_id": str(dg.id), "channel_id": str(channel_id),
                                       "event": self.event})
            out.append(Routed("merged", [k for k, _ in hit], digest_id=str(dg.id),
                              reason="recovered before release"))
            self.changed = True
        return keep, out

    # -- budget ----------------------------------------------------------------

    async def _over_budget(self, db, channel_id: str, keys: list[str]) -> dict[str, int]:
        """{key: messages in the last hour} for recipients at or over budget.
        Counted from durable delivery keys (one per incident event); a
        webhook-style channel (key "") is counted per channel."""
        budget = self.budget
        if not budget:
            return {}
        reserve = bool(self.settings.get("critical_reserve", True))
        crit = (" AND NOT (COALESCE(a.severity, '') = 'critical'"
                " OR COALESCE(a.priority, '') IN ('P1', 'P2'))") if reserve else ""
        named = [k for k in keys if k]
        counts: dict[str, int] = {}
        try:
            async with db.begin_nested():
                if named:
                    rows = (await db.execute(text(
                        "SELECT k.recipient, count(DISTINCT (k.alert_id, k.event))"
                        " FROM alert_delivery_keys k LEFT JOIN alerts a ON a.id = k.alert_id"
                        " WHERE k.recipient = ANY(CAST(:keys AS text[]))"
                        " AND k.state IN ('claimed', 'accepted', 'uncertain')"
                        " AND k.claimed_at >= now() - interval '1 hour'" + crit +
                        " GROUP BY k.recipient"), {"keys": named})).all()
                    counts.update({str(r[0]): int(r[1]) for r in rows})
                if "" in keys:
                    n = (await db.execute(text(
                        "SELECT count(DISTINCT (k.alert_id, k.event))"
                        " FROM alert_delivery_keys k LEFT JOIN alerts a ON a.id = k.alert_id"
                        " WHERE k.recipient = '' AND k.channel_id = CAST(:cid AS uuid)"
                        " AND k.state IN ('claimed', 'accepted', 'uncertain')"
                        " AND k.claimed_at >= now() - interval '1 hour'" + crit),
                        {"cid": str(channel_id)})).scalar()
                    counts[""] = int(n or 0)
        except Exception as exc:
            logger.warning("notification budget: count failed, sending: %s", exc)
            return {}
        return {k: n for k, n in counts.items() if n >= budget}

    # -- holding -------------------------------------------------------------

    async def _hold(self, db: AsyncSession, *, channel_id: str, channel_name: str | None,
                    recipient: str, keys: list[str], reason: str,
                    sent_last_hour: int | None = None) -> Routed:
        from app.services.alert_workflow import record_event

        s = self.settings
        wait = int(s.get("group_wait_seconds") or 0)
        interval = int(s.get("group_interval_seconds") or 300)
        max_items = int(s.get("max_items") or 50)
        if reason == REASON_BUDGET:
            group_key, group_label = BUDGET_GROUP_KEY, "Notification budget"
            first_wait = interval
        else:
            group_key, group_label = self.group_key, self.group_label
            first_wait = wait
        params = {"gk": group_key, "cid": str(channel_id), "rcpt": recipient,
                  "aid": self.alert_id, "ev": self.event}

        # Already held (or summarised) for this event on this route: a retry
        # or a second worker must not list the incident twice.
        dup = (await db.execute(text(
            "SELECT d.id FROM notification_digest_items i JOIN notification_digests d ON d.id = i.digest_id"
            " WHERE i.alert_id = CAST(:aid AS uuid) AND i.event = :ev"
            " AND d.channel_id IS NOT DISTINCT FROM CAST(:cid AS uuid) AND d.recipient = :rcpt"
            " AND d.status IN ('open', 'sending', 'sent') LIMIT 1"), params)).scalar()
        if dup:
            return Routed("duplicate", keys, digest_id=str(dup))

        timing = (await db.execute(text(
            "SELECT now(), GREATEST(now() + make_interval(secs => :wait),"
            " COALESCE((SELECT max(sent_at) FROM notification_digests"
            "  WHERE group_key = :gk AND channel_id IS NOT DISTINCT FROM CAST(:cid AS uuid)"
            "  AND recipient = :rcpt AND status = 'sent'), '-infinity'::timestamptz)"
            " + make_interval(secs => :interval))"),
            {**params, "wait": first_wait, "interval": interval})).first()
        now, release_at = timing[0], timing[1]
        interval_driven = (release_at - now).total_seconds() > first_wait + 1
        condition = self._condition(reason, release_at, wait=first_wait, interval=interval,
                                    max_items=max_items, interval_driven=interval_driven,
                                    sent_last_hour=sent_last_hour)
        row = (await db.execute(text(
            "INSERT INTO notification_digests (group_key, group_label, channel_id, recipient, status,"
            " reason, release_condition, release_at, item_count)"
            " VALUES (:gk, :label, CAST(:cid AS uuid), :rcpt, 'open', :reason, :cond, :rel, 0)"
            " ON CONFLICT (group_key, (COALESCE(channel_id, '00000000-0000-0000-0000-000000000000'::uuid)),"
            " recipient) WHERE status = 'open'"
            " DO UPDATE SET group_label = COALESCE(notification_digests.group_label, EXCLUDED.group_label)"
            " RETURNING id, release_at, release_condition, reason"),
            {**params, "label": group_label, "reason": reason, "cond": condition,
             "rel": release_at})).first()
        digest_id = str(row.id)
        # The upsert holds the digest row lock: re-check under it.
        if (await db.execute(text(
                "SELECT 1 FROM notification_digest_items WHERE digest_id = CAST(:d AS uuid)"
                " AND alert_id = CAST(:aid AS uuid) AND event = :ev"),
                {"d": digest_id, **params})).first():
            return Routed("duplicate", keys, digest_id=digest_id)
        summary = item_summary(self.ctx, self.info, self.priority)
        await db.execute(text(
            "INSERT INTO notification_digest_items (digest_id, alert_id, event, severity, summary)"
            " VALUES (CAST(:d AS uuid), CAST(:aid AS uuid), :ev, :sev, CAST(:sum AS jsonb))"),
            {"d": digest_id, **params, "sev": summary["severity"][:20],
             "sum": json.dumps(summary, default=str)})
        upd = (await db.execute(text(
            "UPDATE notification_digests SET item_count = item_count + 1,"
            " release_at = CASE WHEN item_count + 1 >= :max THEN LEAST(release_at, now())"
            " ELSE release_at END WHERE id = CAST(:d AS uuid) RETURNING release_at, release_condition"),
            {"d": digest_id, "max": max_items})).first()
        human = (f"Grouped with related {self.family_label.lower()} notifications for {self.group_label}"
                 if reason == REASON_GROUPING else
                 f"Hourly notification budget reached for this recipient ({sent_last_hour} sent in "
                 f"the last hour, limit {self.budget})")
        await record_event(db, self.alert_id, "notification_held", source="system", reason=human,
                           detail={"digest_id": digest_id, "reason_code": reason,
                                   "release_condition": upd.release_condition,
                                   "release_at": upd.release_at.isoformat(),
                                   "channel_id": str(channel_id), "channel_name": channel_name,
                                   "group_key": group_key, "group_label": group_label,
                                   "recipients": len([k for k in keys if k]) or None,
                                   "event": self.event})
        self.changed = True
        return Routed("held", keys, digest_id=digest_id, reason=human,
                      release_condition=upd.release_condition)

    @property
    def family_label(self) -> str:
        return FAMILY_LABELS.get(self.family, self.family)

    def _condition(self, reason, release_at, *, wait, interval, max_items, interval_driven,
                   sent_last_hour) -> str:
        if reason == REASON_BUDGET:
            return (f"Released at {_fmt_utc(release_at)} as one summary of every notification "
                    f"held for this recipient over the hourly budget ({self.budget}); "
                    "critical (P1/P2) notifications are never held.")
        why = (f"next update for this group (group interval {interval} s)" if interval_driven
               else f"group wait {wait} s")
        return (f"Released at {_fmt_utc(release_at)} ({why}) as one summary with related warnings, "
                f"or at the next sweep once {max_items} incidents are waiting; critical (P1/P2) "
                "notifications are never held.")


def _lifecycle_event(ctx: dict) -> str:
    return str(ctx.get("delivery_event") or "").strip()[:64]


def _is_recovery(ctx: dict) -> bool:
    ev = _lifecycle_event(ctx)
    return bool(ctx.get("is_recovery") or ctx.get("resolved")
                or str(ctx.get("status") or "").upper() == "RESOLVED" or ev == "recovery")


def _is_holdable_trigger(ctx: dict) -> bool:
    ev = _lifecycle_event(ctx)
    if not ctx.get("groupable") or _is_recovery(ctx):
        return False
    if ctx.get("escalation_level") or ctx.get("delivery_kind") not in (None, "", "trigger"):
        return False
    return ev == "trigger" or ev.startswith("trigger:")


async def gate_for(db: AsyncSession, ctx: dict, keyed) -> Gate | None:
    """The grouping/budget gate for one dispatch, or None when every send of
    it goes out as before (the common case costs no query unless the caller
    marked the send groupable or it is a recovery)."""
    if not keyed:
        return None
    alert_id, event = str(keyed[0]), str(keyed[1])
    recovery = _is_recovery(ctx)
    holdable = _is_holdable_trigger(ctx)
    if not recovery and not holdable:
        return None
    if not await available(db):
        return None
    if recovery:
        try:
            async with db.begin_nested():
                rows = (await db.execute(text(
                    "SELECT d.id, d.channel_id, d.recipient FROM notification_digests d"
                    " WHERE d.status = 'open' AND EXISTS (SELECT 1 FROM notification_digest_items i"
                    "  WHERE i.digest_id = d.id AND i.alert_id = CAST(:aid AS uuid))"
                    " FOR SHARE"), {"aid": alert_id})).all()
        except Exception as exc:
            logger.warning("grouping: open digest lookup failed: %s", exc)
            return None
        return Gate("fold", alert_id, event, fold_digests=list(rows)) if rows else None
    if str(ctx.get("severity") or "").lower() == "critical":
        return None
    settings = await load_settings(db)
    if not settings["enabled"]:
        return None
    if not (int(settings["group_wait_seconds"]) > 0 or int(settings["budget_per_recipient_per_hour"]) > 0):
        return None
    try:
        async with db.begin_nested():
            info = (await db.execute(text(_INFO_SQL), {"id": alert_id})).first()
    except Exception as exc:
        logger.warning("grouping: alert lookup failed, sending: %s", exc)
        return None
    if info is None or info.status == "resolved":
        return None
    if (info.severity or "").lower() == "critical":
        return None
    priority = (prio.normalize_priority(ctx.get("priority"))
                or prio.effective_priority(info.priority, info.rule_priority, info.severity))
    if prio.is_urgent(priority):
        return None
    family = str(ctx.get("event_family") or "") or event_family(
        ctx.get("metric") or info.metric, device=bool(info.device_id), server=bool(info.server_id),
        service=bool(info.service_check_id))
    key, label = group_for(info, list(settings["group_by"]), family)
    return Gate("hold", alert_id, event, settings=settings, ctx=ctx, info=info,
                priority=priority, group_key=key, group_label=label, family=family)


# ── Release ──────────────────────────────────────────────────────────────────

_DIGEST_COLS = ("id, group_key, group_label, channel_id, recipient, status, reason, release_condition,"
                " opened_at, release_at, sent_at, item_count, error")


async def _claim(db: AsyncSession, *, digest_id: str | None = None, limit: int = RELEASE_BATCH) -> list:
    """Move due digests open -> sending (one owner each) and commit."""
    if digest_id:
        where = "id = CAST(:id AS uuid) AND status IN ('open', 'failed')"
    else:
        where = ("(status = 'open' AND release_at <= now()) OR (status = 'sending' AND sent_at IS NULL"
                 f" AND release_at < now() - interval '{STALE_SENDING_MINUTES} minutes')")
    rows = (await db.execute(text(
        f"UPDATE notification_digests SET status = 'sending', error = NULL WHERE id IN ("
        f" SELECT id FROM notification_digests WHERE {where}"
        f" ORDER BY release_at LIMIT :lim FOR UPDATE SKIP LOCKED) RETURNING {_DIGEST_COLS}"),
        {"id": digest_id, "lim": limit})).all()
    await db.commit()
    return list(rows)


async def _items(db: AsyncSession, digest_id) -> list:
    return list((await db.execute(text(
        "SELECT i.id, i.alert_id, i.event, i.severity, i.summary, i.added_at,"
        " a.status AS alert_status, a.resolved_at"
        " FROM notification_digest_items i LEFT JOIN alerts a ON a.id = i.alert_id"
        " WHERE i.digest_id = CAST(:d AS uuid) ORDER BY i.added_at, i.id"), {"d": str(digest_id)})).all())


def _recovered(item) -> bool:
    return item.alert_id is None or item.alert_status in (None, "resolved")


async def _event_all(db, items, kind: str, *, reason: str, detail: dict) -> None:
    from app.services.alert_workflow import record_event
    for aid in dict.fromkeys(str(i.alert_id) for i in items if i.alert_id):
        try:
            async with db.begin_nested():
                await record_event(db, aid, kind, source="system", reason=reason, detail=detail)
        except Exception as exc:
            logger.debug("digest event for %s failed: %s", aid, exc)


async def _finish(db, digest_id, status: str, *, error: str | None = None, sent: bool = False) -> None:
    await db.execute(text(
        "UPDATE notification_digests SET status = :st, error = :err,"
        " sent_at = CASE WHEN :sent THEN now() ELSE sent_at END WHERE id = CAST(:d AS uuid)"),
        {"st": status, "err": error, "sent": sent, "d": str(digest_id)})


async def _release_one(db: AsyncSession, d, dispatch: Callable | None = None) -> str:
    from app.services import host_alert_service as has

    items = await _items(db, d.id)
    active = [i for i in items if not _recovered(i)]
    detail = {"digest_id": str(d.id), "items": len(items), "group_label": d.group_label}
    if not items:
        await _finish(db, d.id, "cancelled", error="Nothing was held in this digest.")
        await db.commit()
        return "cancelled"
    if not active:
        why = (f"All {len(items)} held incident{'s' if len(items) != 1 else ''} recovered before "
               "release; nothing was sent.")
        await _finish(db, d.id, "cancelled", error=why)
        await _event_all(db, items, "notification_cancelled", reason=why, detail=detail)
        await db.commit()
        return "cancelled"
    settings = await load_settings(db)
    ctx = await digest_context(db, d, items, max_items=int(settings["max_items"]))
    channel_ids: list = []
    if str(d.channel_id or "") == DIRECT_CHANNEL_ID:
        kind, _, address = (d.recipient or "").partition(":")
        ctx["recipients"] = [{"kind": kind, "address": address, "user_id": None, "label": None}]
    else:
        channel_ids = [str(d.channel_id)] if d.channel_id else []
        if d.recipient:
            ctx["only_recipients"] = d.recipient.split(",")
    send = dispatch or has.dispatch_to_channels
    try:
        n = await send(db, channel_ids, ctx)
    except Exception as exc:
        await db.rollback()
        logger.warning("digest %s: send failed: %s", d.id, exc)
        n, err = 0, f"send failed: {str(exc)[:200]}"
    else:
        err = None if n else ("No channel accepted the summary (channel disabled, no recipients or "
                              "no enabled gateway); see the delivery log of the first incident.")
    if n:
        await _finish(db, d.id, "sent", sent=True)
        await _event_all(db, items, "notification_released",
                         reason=f"Sent as one summary of {len(items)} incidents",
                         detail={**detail, "active": len(active), "recovered": len(items) - len(active)})
        await db.commit()
        return "sent"
    await _finish(db, d.id, "failed", error=err)
    await _event_all(db, items, "notification_release_failed", reason=err, detail=detail)
    await db.commit()
    return "failed"


async def release_due(db: AsyncSession, *, dispatch: Callable | None = None) -> dict:
    """Send (or cancel) every digest whose release time has passed."""
    if not await available(db):
        return {"claimed": 0}
    counts = {"claimed": 0, "sent": 0, "failed": 0, "cancelled": 0}
    while True:
        batch = await _claim(db)
        if not batch:
            break
        for d in batch:
            counts["claimed"] += 1
            try:
                counts[await _release_one(db, d, dispatch)] += 1
            except Exception:
                await db.rollback()
                logger.exception("digest %s release failed", d.id)
                try:
                    await _finish(db, d.id, "failed", error="release failed; see the server log")
                    await db.commit()
                except Exception:
                    await db.rollback()
                counts["failed"] += 1
        if len(batch) < RELEASE_BATCH:
            break
    return counts


class ReleaseConflict(Exception):
    pass


async def release_now(db: AsyncSession, digest_id: str, *, dispatch: Callable | None = None) -> str:
    """Operator action: send an open (or failed) digest immediately."""
    batch = await _claim(db, digest_id=str(digest_id), limit=1)
    if not batch:
        status = (await db.execute(text("SELECT status FROM notification_digests WHERE id = CAST(:d AS uuid)"),
                                   {"d": str(digest_id)})).scalar()
        if status is None:
            raise LookupError("digest not found")
        raise ReleaseConflict(f"digest is {status}" if status != "open" else "digest is being sent")
    await db.execute(text("UPDATE notification_digests SET release_at = LEAST(release_at, now())"
                          " WHERE id = CAST(:d AS uuid)"), {"d": str(digest_id)})
    await db.commit()
    return await _release_one(db, batch[0], dispatch)


async def release_tick(session_factory=None) -> None:
    """One release pass from the escalation loop, in one worker at a time.
    Never raises (except cancellation): a failure here must not stop the loop."""
    import asyncio

    from app.services.alert_worker_lock import NOTIFICATION_DIGEST_PASS, run_pass
    try:
        await run_pass(NOTIFICATION_DIGEST_PASS, release_due, session_factory)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("notification digest sweep failed")


# ── Digest message ───────────────────────────────────────────────────────────

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def digest_context(db: AsyncSession, d, items: list, *, max_items: int = 50) -> dict:
    """The dispatch ctx of a digest: subject, text, SMS line, HTML and a
    structured webhook payload, all listing every child incident."""
    from app.api.v1.alert_engine import _dashboard_url

    active = [i for i in items if not _recovered(i)]
    recovered = [i for i in items if _recovered(i)]
    sev = max((str((i.summary or {}).get("severity") or i.severity or "warning").lower() for i in active),
              key=lambda s: _SEV_RANK.get(s, 1), default="warning")
    prios = [prio.normalize_priority((i.summary or {}).get("priority")) for i in active]
    priority = min((p for p in prios if p), key=prio.priority_rank, default=prio.severity_priority(sev))
    noun = {"info": "notice", "critical": "alert"}.get(sev, "warning")
    label = d.group_label or "related incidents"
    base = await _dashboard_url(db, "/")
    base = base.rstrip("/") if base else ""

    def link(path: str) -> str:
        return f"{base}{path}" if base and path else ""

    rows = []
    for i in items:
        s = dict(i.summary or {})
        s["recovered"] = _recovered(i)
        s["alert_id"] = str(i.alert_id) if i.alert_id else None
        s["url"] = link(s.get("path") or (f"/alerts/{i.alert_id}" if i.alert_id else ""))
        rows.append(s)
    head = f"{_plural(len(active), noun)} — {label}"
    extra = f" ({len(recovered)} already recovered)" if recovered else ""
    subject = f"[{sev.upper()}] [{priority}] [Digest] {head}{extra}"
    reason_line = ("These notifications were held because the recipient reached the hourly "
                   "notification budget." if d.reason == REASON_BUDGET else
                   "These related notifications were held briefly and sent together.")
    lines = [f"{head}{extra}", "", reason_line, ""]
    for s in rows:
        if s["recovered"]:
            continue
        lines.append(f"- {s.get('incident_ref', '')} {s.get('asset', '')}: {s.get('condition', '')}"
                     + (f" ({s['value']})" if s.get("value") else "")
                     + (f" — first seen {s['first_seen']}" if s.get("first_seen") else "")
                     + (f" {s['url']}" if s.get("url") else ""))
    if recovered:
        lines += ["", "Already recovered:"]
        lines += [f"- {s.get('incident_ref', '')} {s.get('asset', '')}: {s.get('condition', '')}"
                  for s in rows if s["recovered"]]
    body = "\n".join(lines)
    digest_url = link(f"/escalation?tab=grouping&digest={d.id}")
    sms_bits = [f"{s.get('asset', '')} {s.get('condition', '')} {s.get('value', '')}".strip()
                for s in rows if not s["recovered"]][:3]
    sms = f"ZenPlus [{priority}] {head}: " + "; ".join(sms_bits)
    if len(active) > 3:
        sms += f"; +{len(active) - 3} more"
    if recovered:
        sms += f". {len(recovered)} recovered"
    if digest_url:
        sms += f". {digest_url}"
    anchor = next((str(i.alert_id) for i in items if i.alert_id), None)
    return {
        "subject": subject,
        "body": body,
        "message": sms[:480],
        "html_body": digest_email_html(rows, head=head, extra=extra, severity=sev, priority=priority,
                                       reason_line=reason_line, digest_url=digest_url,
                                       max_items=max_items),
        "rule_name": f"Digest: {label}",
        "hostname": label,
        "ip_address": "",
        "status": f"ALERTING ({_plural(len(active), 'incident')})",
        "severity": sev,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "action_url": digest_url,
        "alert_id": anchor,
        "delivery_event": f"digest:{d.id}",
        "delivery_kind": "digest",
        "groupable": False,
        "dedup_key": f"zenplus:digest:{d.id}",
        "type": "digest",
        "digest": {"id": str(d.id), "group_key": d.group_key, "group_label": d.group_label,
                   "reason": d.reason, "priority": priority, "items": rows},
    }


def digest_email_html(rows: list[dict], *, head: str, extra: str, severity: str, priority: str,
                      reason_line: str, digest_url: str, max_items: int = 50) -> str:
    """One summary mail in the house style (email_render's shell)."""
    from app.services import email_render as er

    accent = er.severity_hex(severity)
    tint = er._severity_tint(severity)[0]
    esc = er._esc

    def table(items: list[dict], *, muted: bool = False) -> str:
        cells = []
        for s in items:
            ref = esc(s.get("incident_ref") or "")
            if s.get("url"):
                ref = f'<a href="{esc(s["url"])}" style="color:{er._BRAND};text-decoration:none;">{ref}</a>'
            color = er._MUTED if muted else er._INK
            cells.append(
                f'<tr><td style="padding:8px 10px;border-top:1px solid {er._BORDER};font-size:12px;'
                f'font-weight:700;white-space:nowrap;vertical-align:top;">{ref}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {er._BORDER};font-size:13px;color:{color};'
                f'vertical-align:top;"><b>{esc(s.get("asset") or "")}</b><br>{esc(s.get("condition") or "")}'
                + (f' &middot; {esc(s["value"])}' if s.get("value") else "") +
                f'</td><td style="padding:8px 10px;border-top:1px solid {er._BORDER};font-size:12px;'
                f'color:{er._MUTED};white-space:nowrap;vertical-align:top;">{er._fmt_ts(s.get("first_seen"))}</td></tr>')
        hdr = (f'<tr><td style="padding:8px 10px;font-size:10px;font-weight:700;letter-spacing:.6px;'
               f'text-transform:uppercase;color:{er._MUTED};">Incident</td>'
               f'<td style="padding:8px 10px;font-size:10px;font-weight:700;letter-spacing:.6px;'
               f'text-transform:uppercase;color:{er._MUTED};">Asset and condition</td>'
               f'<td style="padding:8px 10px;font-size:10px;font-weight:700;letter-spacing:.6px;'
               f'text-transform:uppercase;color:{er._MUTED};">First seen</td></tr>')
        return (f'  <tr><td style="padding:14px 28px 0;"><table role="presentation" width="100%" '
                f'cellpadding="0" cellspacing="0" style="background:{er._PANEL};border:1px solid '
                f'{er._BORDER};border-radius:10px;border-collapse:separate;">{hdr}{"".join(cells)}'
                f'</table></td></tr>')

    active = [s for s in rows if not s.get("recovered")]
    recovered = [s for s in rows if s.get("recovered")]
    shown = active[:max_items]
    more = len(active) - len(shown)
    inner = (er._header("ZenPlus", chip_label="DIGEST", chip_color=er._STATUS_CHIP,
                        severity_label=f"{severity.upper()} · {priority}", severity_color=accent)
             + er._hero(esc(head), esc(extra.strip(" ()") or "Summary of held notifications"),
                        icon="Σ", accent=accent)
             + er._callout(esc(reason_line), accent=accent, tint=tint)
             + table(shown))
    if more > 0:
        inner += er._notice(f"{more} more incident{'s' if more != 1 else ''} in this summary — "
                            "open the digest to see all of them.")
    if recovered:
        inner += (f'  <tr><td style="padding:18px 28px 0;font-size:12px;font-weight:700;'
                  f'letter-spacing:.6px;text-transform:uppercase;color:{er._MUTED};">'
                  f'Already recovered ({len(recovered)})</td></tr>' + table(recovered[:max_items], muted=True))
    inner += er._button(digest_url, "View digest", color=er._BRAND)
    inner += er._footer("ZenPlus", "This summary replaces the individual notifications listed above. "
                                   "Critical (P1/P2) notifications are never held.")
    return er._shell(inner, title=head, preheader=head, accent=accent)


# ── Read models (API / alert detail) ─────────────────────────────────────────

def recipient_label(channel_id, recipient: str) -> str:
    if str(channel_id or "") == DIRECT_CHANNEL_ID:
        return (recipient or "").partition(":")[2] or recipient or ""
    if not recipient:
        return "All channel recipients"
    if recipient == BUDGET_GROUP_KEY:
        return recipient
    return ", ".join(recipient.split(","))


def digest_payload(r) -> dict:
    return {
        "id": str(r.id), "group_key": r.group_key, "group_label": r.group_label,
        "channel_id": str(r.channel_id) if r.channel_id else None,
        "channel_name": (DIRECT_CHANNEL_NAME if str(r.channel_id or "") == DIRECT_CHANNEL_ID
                         else getattr(r, "channel_name", None)),
        "channel_type": getattr(r, "channel_type", None),
        "recipient": recipient_label(r.channel_id, r.recipient),
        "status": r.status, "reason": r.reason, "release_condition": r.release_condition,
        "status_detail": r.error, "opened_at": r.opened_at, "release_at": r.release_at,
        "sent_at": r.sent_at, "item_count": int(r.item_count or 0),
    }


async def alert_holds(db: AsyncSession, alert_id) -> list[dict]:
    if not await available(db):
        return []
    rows = (await db.execute(text(
        "SELECT d.id, d.reason, d.release_condition, d.release_at, d.status, d.sent_at, d.error,"
        " d.group_label, d.channel_id, c.name AS channel_name, i.event, i.added_at"
        " FROM notification_digest_items i JOIN notification_digests d ON d.id = i.digest_id"
        " LEFT JOIN notification_channels c ON c.id = d.channel_id"
        " WHERE i.alert_id = CAST(:id AS uuid) ORDER BY i.added_at, d.id"), {"id": str(alert_id)})).all()
    return [{"digest_id": str(r.id), "reason": r.reason, "release_condition": r.release_condition,
             "release_at": r.release_at, "status": r.status, "sent_at": r.sent_at,
             "status_detail": r.error, "group_label": r.group_label, "event": r.event, "held_at": r.added_at,
             "channel_name": (DIRECT_CHANNEL_NAME if str(r.channel_id or "") == DIRECT_CHANNEL_ID
                              else r.channel_name)}
            for r in rows]


async def attach_alert_holds(db: AsyncSession, payload: dict, alert_id) -> None:
    """Alert detail: ``notification_holds`` plus readable timeline labels for
    the grouping events."""
    try:
        payload["notification_holds"] = await alert_holds(db, alert_id)
    except Exception as exc:
        logger.warning("notification holds unavailable for %s: %s", alert_id, exc)
        payload["notification_holds"] = []
    for e in payload.get("events") or []:
        if e.get("kind") in EVENT_LABELS:
            e["label"] = EVENT_LABELS[e["kind"]]


def is_uuid(value) -> bool:
    return bool(re.match(_UUID_RE, str(value or "")))
