"""Alert storm (correlation) policy: one combined notification per storm.

A site losing power, a core switch failing or a mass reboot after a firmware
push raises the same event on dozens of assets within a minute or two. Without
this policy every channel receives one message per asset. With it enabled:

* Each trigger of a selected event family (``settings['families']``: device
  down / unreachable, reboots and restarts, service checks, ...) is recorded
  once per incident in ``alert_storm_events`` under a storm key: the family
  plus the configured scope (all assets, or per site, device group, scope tag
  or monitoring appliance).
* While the number of DISTINCT assets with such a trigger in the last
  ``window_minutes`` is at most ``threshold``, notifications go out exactly as
  before (individually, through grouping and escalation as configured).
* The trigger that makes it MORE than ``threshold`` declares a storm. From
  then on every per-asset notification of that family and scope is held: its
  trigger, its recovery (when its trigger was held) and, with
  ``hold_escalations``, its escalation steps and reminders. Each channel and
  recipient that would have been notified gets ONE combined notification
  (about 30 s after the storm is declared, so a burst lands in one message)
  listing every affected asset, then an update every ``update_minutes`` while
  the storm continues, and a closing summary once no new event of that family
  arrived in the scope for ``window_minutes``. The closing summary says how
  many recovered and which are still down.
* Incidents notified individually before the storm was declared keep their
  own recovery notifications (a pager that received "down" also gets "up").

The held sends ride on the grouping digests (notification_grouping): a hold is
a digest item, the combined notification is a digest with reason ``storm``
sent through ``host_alert_service.dispatch_to_channels`` with its own delivery
key, so every channel type (email, SMS, webhook, Slack, Teams, Discord,
PagerDuty, direct-to-person) gets it once. PagerDuty receives one incident per
storm (dedup key ``zenplus:storm:<id>``), resolved by the closing summary when
every member recovered.

Settings: ``system_settings['alert_storm']``, OFF by default.
Schema: scripts/migrate-184-alert-storm-policy.sql.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_priority as prio
from app.services import notification_grouping as ng

logger = logging.getLogger("zenplus.alert_storm")

SETTINGS_KEY = "alert_storm"
REASON_STORM = "storm"
SCOPE_CHOICES: tuple[str, ...] = ("all", "site", "device_group", "scope_tag", "appliance")
FAMILY_CHOICES: tuple[str, ...] = ("reachability", "reboot", "service", "interface", "latency",
                                   "hardware", "network", "process", "trap", "syslog")
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "threshold": 5,
    "window_minutes": 5,
    "families": ["reachability", "reboot"],
    "scope": "all",
    "update_minutes": 15,
    "hold_escalations": True,
}
LIMITS = {"threshold": (2, 1000), "window_minutes": (1, 120), "update_minutes": (5, 240)}
# The first combined notification waits this long after the storm is declared
# so the rest of a burst lands in the same message.
FIRST_WAIT_SECONDS = 30
# Storm events older than this are pruned (they only feed the sliding window).
EVENT_RETENTION_DAYS = 7

# What happened, per family: (plural noun, past-tense phrase).
FAMILY_PHRASES = {
    "reachability": ("devices", "went down or became unreachable"),
    "reboot": ("devices", "rebooted or restarted"),
    "service": ("service checks", "failed"),
    "interface": ("devices", "raised interface alerts"),
    "latency": ("devices", "raised latency or packet-loss alerts"),
    "hardware": ("devices", "raised hardware alerts"),
    "network": ("devices", "raised routing, VPN or HA alerts"),
    "process": ("servers", "reported stopped services or processes"),
    "trap": ("devices", "sent alerting SNMP traps"),
    "syslog": ("devices", "logged alerting syslog messages"),
}
FAMILY_TITLES = {
    "reachability": "Devices down", "reboot": "Reboots and restarts", "service": "Service checks failing",
    "interface": "Interface alerts", "latency": "Latency and packet loss", "hardware": "Hardware alerts",
    "network": "Routing, VPN and HA", "process": "Server services and processes",
    "trap": "SNMP traps", "syslog": "Syslog alerts",
}


# ── Settings ─────────────────────────────────────────────────────────────────

def clean_settings(raw: Any) -> dict[str, Any]:
    out = {**DEFAULT_SETTINGS, "families": list(DEFAULT_SETTINGS["families"])}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        return out
    for key in ("enabled", "hold_escalations"):
        if key in raw:
            out[key] = bool(raw[key])
    if isinstance(raw.get("families"), (list, tuple)):
        wanted = {str(x) for x in raw["families"]}
        out["families"] = [f for f in FAMILY_CHOICES if f in wanted]
    if raw.get("scope") in SCOPE_CHOICES:
        out["scope"] = raw["scope"]
    for key, (lo, hi) in LIMITS.items():
        if key in raw:
            out[key] = ng._int_in(raw[key], lo, hi, DEFAULT_SETTINGS[key])
    return out


async def load_settings(db: AsyncSession) -> dict[str, Any]:
    try:
        async with db.begin_nested():
            row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                    {"k": SETTINGS_KEY})).first()
    except Exception as exc:
        logger.warning("storm settings unreadable, storm policy off: %s", exc)
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


def policy_sentence(s: dict) -> str:
    if not s.get("enabled"):
        return "Off: every asset is notified on its own."
    fams = [FAMILY_TITLES.get(f, f).lower() for f in s.get("families") or []]
    if not fams:
        return "On, but no event type is selected, so nothing is combined."
    scope = {"all": "across all assets", "site": "per site", "device_group": "per device group",
             "scope_tag": "per scope tag", "appliance": "per monitoring appliance"}[s.get("scope", "all")]
    return (f"When more than {s['threshold']} assets report {', '.join(fams)} within {s['window_minutes']} "
            f"min ({scope}), each channel gets one combined notification instead of one per asset, "
            f"then an update every {s['update_minutes']} min and a closing summary.")


_TABLES_OK: bool | None = None


async def available(db: AsyncSession) -> bool:
    global _TABLES_OK
    if _TABLES_OK:
        return True
    try:
        async with db.begin_nested():
            ok = (await db.execute(text(
                "SELECT to_regclass('public.alert_storms') IS NOT NULL"
                " AND to_regclass('public.alert_storm_members') IS NOT NULL"
                " AND to_regclass('public.alert_storm_events') IS NOT NULL"))).scalar()
    except Exception:
        ok = False
    _TABLES_OK = bool(ok) and await ng.available(db)
    return _TABLES_OK


def reset_for_tests() -> None:
    global _TABLES_OK
    _TABLES_OK = None


# ── Classification ───────────────────────────────────────────────────────────

def send_kind(ctx: dict) -> str | None:
    """"trigger" | "recovery" | "escalation" for a per-incident send, or None
    for sends this policy never touches (summaries, notices, tests)."""
    if ctx.get("storm") or ctx.get("delivery_kind") == "digest" or ctx.get("type") == "digest":
        return None
    ev = ng._lifecycle_event(ctx)
    if not ev or ev.startswith(("digest:", "storm:")):
        return None
    if ng._is_recovery(ctx):
        return "recovery"
    if ctx.get("escalation_level") or ctx.get("delivery_kind") in ("escalation", "reminder", "review") \
            or ev.startswith(("escalation", "reminder", "review", "repeat")):
        return "escalation"
    if ev == "trigger" or ev.startswith("trigger:"):
        return "trigger"
    return None


def asset_key(info) -> str | None:
    if info.device_id:
        return f"device:{info.device_id}"
    if info.server_id:
        return f"server:{info.server_id}"
    if info.service_check_id:
        return f"service:{info.service_check_id}"
    return None


def scope_for(info, scope: str) -> tuple[str, str]:
    """(scope key, label) — "" label for all assets."""
    if scope == "all":
        return "all", ""
    key, label = ng.group_for(info, [scope], "other")
    return key, label


# ── The gate ─────────────────────────────────────────────────────────────────

@dataclass
class StormGate:
    """Holds every route of one per-incident send into the storm's combined
    notification. Same interface as notification_grouping.Gate."""
    storm_id: str
    alert_id: str
    event: str
    kind: str
    family: str
    scope_label: str
    first: bool = False
    update_minutes: int = 15
    ctx: dict = field(default_factory=dict)
    info: Any = None
    priority: str = "P3"
    changed: bool = False

    async def route(self, db: AsyncSession, *, channel_id: str, channel_name: str | None,
                    channel_type: str, targets: list[tuple[str, str]],
                    direct_kind: str | None = None) -> tuple[list[tuple[str, str]], list]:
        if not targets:
            return targets, []
        try:
            async with db.begin_nested():
                if direct_kind:
                    out = [await self._hold(db, channel_id, channel_name, ng._direct_recipient(direct_kind, k), [k])
                           for k, _a in targets]
                else:
                    recipient = ",".join(sorted(k for k, _ in targets if k))
                    out = [await self._hold(db, channel_id, channel_name, recipient, [k for k, _ in targets])]
        except Exception as exc:
            # Never lose a notification to the storm gate: send it as before.
            logger.warning("storm gate failed for alert %s, sending: %s", self.alert_id, exc)
            return targets, []
        return [], out

    async def _hold(self, db: AsyncSession, channel_id, channel_name, recipient: str, keys: list[str]):
        from app.services.alert_workflow import record_event

        group_key = f"storm:{self.storm_id}"
        params = {"gk": group_key, "cid": str(channel_id), "rcpt": recipient,
                  "aid": self.alert_id, "ev": self.event}
        dup = (await db.execute(text(
            "SELECT d.id FROM notification_digest_items i JOIN notification_digests d ON d.id = i.digest_id"
            " WHERE i.alert_id = CAST(:aid AS uuid) AND i.event = :ev AND d.group_key LIKE :gk || '%'"
            " AND d.channel_id IS NOT DISTINCT FROM CAST(:cid AS uuid) AND d.recipient = :rcpt"
            " AND d.status IN ('open', 'sending', 'sent') LIMIT 1"), params)).scalar()
        if dup:
            return ng.Routed("duplicate", keys, digest_id=str(dup))
        # First combined notification on this route: shortly after the storm
        # was declared; later ones: one update interval after the last.
        release_at = (await db.execute(text(
            "SELECT GREATEST(now() + make_interval(secs => :first),"
            " COALESCE((SELECT max(sent_at) FROM notification_digests WHERE group_key = :gk"
            "  AND channel_id IS NOT DISTINCT FROM CAST(:cid AS uuid) AND recipient = :rcpt"
            "  AND status = 'sent'), '-infinity'::timestamptz) + make_interval(mins => :upd))"),
            {**params, "first": FIRST_WAIT_SECONDS, "upd": self.update_minutes})).scalar()
        what = FAMILY_TITLES.get(self.family, self.family)
        label = f"Alert storm: {what}" + (f" — {self.scope_label}" if self.scope_label else "")
        condition = ("Part of an alert storm: this asset is listed in one combined notification (sent "
                     f"about {FIRST_WAIT_SECONDS} s after the storm started, then every "
                     f"{self.update_minutes} min while it lasts) instead of being notified on its own.")
        row = (await db.execute(text(
            "INSERT INTO notification_digests (group_key, group_label, channel_id, recipient, status,"
            " reason, release_condition, release_at, item_count)"
            " VALUES (:gk, :label, CAST(:cid AS uuid), :rcpt, 'open', :reason, :cond, :rel, 0)"
            " ON CONFLICT (group_key, (COALESCE(channel_id, '00000000-0000-0000-0000-000000000000'::uuid)),"
            " recipient) WHERE status = 'open'"
            " DO UPDATE SET group_label = notification_digests.group_label RETURNING id"),
            {**params, "label": label, "reason": REASON_STORM, "cond": condition, "rel": release_at})).first()
        digest_id = str(row.id)
        summary = ng.item_summary(self.ctx, self.info, self.priority)
        summary["storm_event"] = self.kind
        await db.execute(text(
            "INSERT INTO notification_digest_items (digest_id, alert_id, event, severity, summary)"
            " VALUES (CAST(:d AS uuid), CAST(:aid AS uuid), :ev, :sev, CAST(:sum AS jsonb))"),
            {"d": digest_id, **params, "sev": summary["severity"][:20],
             "sum": json.dumps(summary, default=str)})
        await db.execute(text("UPDATE notification_digests SET item_count = item_count + 1"
                              " WHERE id = CAST(:d AS uuid)"), {"d": digest_id})
        human = {"trigger": "Notification replaced by the alert storm summary",
                 "recovery": "Recovery reported in the alert storm summary",
                 "escalation": "Escalation folded into the alert storm summary"}[self.kind]
        await record_event(db, self.alert_id, "notification_held", source="system", reason=human,
                           detail={"digest_id": digest_id, "reason_code": REASON_STORM,
                                   "storm_id": self.storm_id, "release_condition": condition,
                                   "release_at": release_at.isoformat() if release_at else None,
                                   "channel_id": str(channel_id), "channel_name": channel_name,
                                   "event": self.event})
        self.changed = True
        return ng.Routed("held", keys, digest_id=digest_id, reason=human, release_condition=condition)


async def _info(db: AsyncSession, alert_id: str):
    return (await db.execute(text(ng._INFO_SQL), {"id": alert_id})).first()


def _family(ctx: dict, info) -> str:
    return str(ctx.get("event_family") or "") or ng.event_family(
        ctx.get("metric") or info.metric, device=bool(info.device_id), server=bool(info.server_id),
        service=bool(info.service_check_id))


async def gate_for(db: AsyncSession, ctx: dict, keyed) -> StormGate | None:
    """The storm gate for one dispatch, or None to send as usual. Never
    raises: any failure sends the notification as before."""
    if not keyed:
        return None
    kind = send_kind(ctx)
    if kind is None:
        return None
    try:
        if not await available(db):
            return None
        settings = await load_settings(db)
        if kind == "trigger" and (not settings["enabled"] or not settings["families"]):
            return None
        wrote: list[bool] = []
        # A savepoint: a failure here undoes only the storm bookkeeping, never
        # the caller's work. The commit makes the decision durable and ends
        # the per-key advisory lock.
        async with db.begin_nested():
            if kind == "trigger":
                gate = await _trigger_gate(db, ctx, str(keyed[0]), str(keyed[1]), settings, wrote)
            else:
                gate = await _member_gate(db, ctx, str(keyed[0]), str(keyed[1]), kind, settings, wrote)
        if wrote:
            await db.commit()
        return gate
    except Exception as exc:
        logger.warning("storm gate: lookup failed, sending: %s", exc)
        return None


def _priority(ctx, info) -> str:
    return (prio.normalize_priority(ctx.get("priority"))
            or prio.effective_priority(info.priority, info.rule_priority, info.severity))


async def _trigger_gate(db, ctx, alert_id, event, settings, wrote: list) -> StormGate | None:
    info = await _info(db, alert_id)
    if info is None or info.status == "resolved":
        return None
    family = _family(ctx, info)
    akey = asset_key(info)
    if family not in settings["families"] or akey is None:
        return None
    scope_key, scope_label = scope_for(info, settings["scope"])
    storm_key = f"{family}|{scope_key}"
    asset = info.asset or ctx.get("hostname") or ""
    # Serialize decisions per storm key across workers until commit.
    wrote.append(True)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('alert_storm:' || :k))"), {"k": storm_key})
    await db.execute(text(
        "INSERT INTO alert_storm_events (alert_id, storm_key, family, asset_key, asset_label)"
        " VALUES (CAST(:aid AS uuid), :k, :f, :ak, :al) ON CONFLICT (alert_id) DO NOTHING"),
        {"aid": alert_id, "k": storm_key, "f": family, "ak": akey, "al": asset[:255]})
    storm = (await db.execute(text(
        "SELECT id FROM alert_storms WHERE storm_key = :k AND status = 'active'"), {"k": storm_key})).first()
    first = False
    if storm is None:
        window = int(settings["window_minutes"])
        assets = (await db.execute(text(
            "SELECT count(DISTINCT asset_key) FROM alert_storm_events WHERE storm_key = :k"
            " AND at >= now() - make_interval(mins => :w)"), {"k": storm_key, "w": window})).scalar()
        if int(assets or 0) <= int(settings["threshold"]):
            return None
        storm_id = (await db.execute(text(
            "INSERT INTO alert_storms (storm_key, family, scope_label, threshold, window_minutes, started_at)"
            " VALUES (:k, :f, :sl, :t, :w, (SELECT min(at) FROM alert_storm_events WHERE storm_key = :k"
            "  AND at >= now() - make_interval(mins => :w))) RETURNING id"),
            {"k": storm_key, "f": family, "sl": scope_label or None, "t": int(settings["threshold"]),
             "w": window})).scalar()
        # Everything already in the window joins as notified individually.
        await db.execute(text(
            "INSERT INTO alert_storm_members (storm_id, alert_id, asset_key, asset_label, held, joined_at)"
            " SELECT CAST(:s AS uuid), e.alert_id, e.asset_key, e.asset_label, false, e.at"
            " FROM alert_storm_events e WHERE e.storm_key = :k AND e.at >= now() - make_interval(mins => :w)"
            " AND e.alert_id <> CAST(:aid AS uuid) ON CONFLICT DO NOTHING"),
            {"s": str(storm_id), "k": storm_key, "w": window, "aid": alert_id})
        first = True
    else:
        storm_id = storm.id
    await db.execute(text(
        "INSERT INTO alert_storm_members (storm_id, alert_id, asset_key, asset_label, held)"
        " VALUES (CAST(:s AS uuid), CAST(:aid AS uuid), :ak, :al, true)"
        " ON CONFLICT (storm_id, alert_id) DO UPDATE SET held = true"),
        {"s": str(storm_id), "aid": alert_id, "ak": akey, "al": asset[:255]})
    await db.execute(text("UPDATE alert_storms SET last_event_at = now() WHERE id = CAST(:s AS uuid)"),
                     {"s": str(storm_id)})
    return StormGate(str(storm_id), alert_id, event, "trigger", family, scope_label, first=first,
                     update_minutes=int(settings["update_minutes"]), ctx=ctx, info=info,
                     priority=_priority(ctx, info))


async def _member_gate(db, ctx, alert_id, event, kind, settings, wrote: list) -> StormGate | None:
    """Recoveries and escalations of incidents whose trigger a running storm
    replaced."""
    member = (await db.execute(text(
        "SELECT s.id, s.family, s.scope_label, s.status FROM alert_storm_members m"
        " JOIN alert_storms s ON s.id = m.storm_id WHERE m.alert_id = CAST(:aid AS uuid) AND m.held"
        " ORDER BY s.started_at DESC LIMIT 1"), {"aid": alert_id})).first()
    if member is None:
        return None
    if kind == "recovery":
        wrote.append(True)
        await db.execute(text(
            "UPDATE alert_storm_members SET recovered_at = COALESCE(recovered_at, now())"
            " WHERE alert_id = CAST(:aid AS uuid)"), {"aid": alert_id})
    if member.status != "active":
        return None          # the storm is over: report on its own
    if kind == "escalation" and not settings["hold_escalations"]:
        return None
    info = await _info(db, alert_id)
    if info is None:
        return None
    return StormGate(str(member.id), alert_id, event, kind, member.family, member.scope_label or "",
                     update_minutes=int(settings["update_minutes"]), ctx=ctx, info=info,
                     priority=_priority(ctx, info))


# ── Ending storms ────────────────────────────────────────────────────────────

async def sweep(db: AsyncSession) -> dict:
    """End storms that went quiet and queue their closing summaries; prune
    old window events. Called from the digest release pass."""
    if not await available(db):
        return {}
    ended = (await db.execute(text(
        "UPDATE alert_storms SET status = 'ended', ended_at = now() WHERE status = 'active'"
        " AND last_event_at < now() - make_interval(mins => window_minutes) RETURNING id"))).all()
    for row in ended:
        sid = str(row.id)
        gk, final = f"storm:{sid}", f"storm:{sid}:final"
        # A pending update becomes the closing summary of its route.
        await db.execute(text(
            "UPDATE notification_digests SET group_key = :fk, release_at = now()"
            " WHERE group_key = :gk AND status = 'open'"), {"gk": gk, "fk": final})
        # Routes that already received the storm get a closing summary too.
        await db.execute(text(
            "INSERT INTO notification_digests (group_key, group_label, channel_id, recipient, status,"
            " reason, release_condition, release_at, item_count)"
            " SELECT DISTINCT ON (channel_id, recipient) :fk, group_label, channel_id, recipient, 'open',"
            " :reason, 'Closing summary of the alert storm.', now(), 0 FROM notification_digests"
            " WHERE group_key = :gk AND status IN ('sent', 'sending', 'failed')"
            " ON CONFLICT (group_key, (COALESCE(channel_id, '00000000-0000-0000-0000-000000000000'::uuid)),"
            " recipient) WHERE status = 'open' DO NOTHING"), {"gk": gk, "fk": final, "reason": REASON_STORM})
    await db.execute(text(
        f"DELETE FROM alert_storm_events WHERE at < now() - interval '{EVENT_RETENTION_DAYS} days'"))
    await db.commit()
    return {"ended": len(ended)}


# ── The combined notification ────────────────────────────────────────────────

def storm_id_of(group_key: str) -> str:
    return str(group_key or "").split(":")[1] if str(group_key or "").startswith("storm:") else ""


async def _members(db: AsyncSession, storm_id: str) -> list:
    return list((await db.execute(text(
        "SELECT m.alert_id, m.asset_label, m.held, m.joined_at, m.recovered_at,"
        " a.status AS alert_status, a.resolved_at, a.severity, a.priority, a.triggered_at, a.message"
        " FROM alert_storm_members m LEFT JOIN alerts a ON a.id = m.alert_id"
        " WHERE m.storm_id = CAST(:s AS uuid) ORDER BY m.joined_at, m.asset_label"),
        {"s": storm_id})).all())


def _member_recovered(m) -> bool:
    return m.alert_status in (None, "resolved")


def _names(rows, limit: int = 3) -> str:
    names = [r for r in rows if r][:limit]
    more = len([r for r in rows if r]) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


async def storm_context(db: AsyncSession, d, storm, members: list, items: list, *, final: bool,
                        first: bool) -> dict:
    from app.api.v1.alert_engine import _dashboard_url
    from app.services.alert_phrasing import incident_ref
    from app.services.regional_time import timestamp_label

    branding = await ng.notification_branding(db)
    noun, verb = FAMILY_PHRASES.get(storm.family, ("assets", "raised alerts"))
    where = f" — {storm.scope_label}" if storm.scope_label else ""
    down = [m for m in members if not _member_recovered(m)]
    up = [m for m in members if _member_recovered(m)]
    total = len(members)
    since = timestamp_label(storm.started_at, branding)
    base = (await _dashboard_url(db, "/") or "").rstrip("/")
    storm_url = f"{base}/escalation?tab=storms&storm={storm.id}" if base else ""
    new_ids = {str(i.alert_id) for i in items if (i.summary or {}).get("storm_event") == "trigger"}
    sev = "critical" if any((m.severity or "") == "critical" for m in down) else ("info" if final and not down else "warning")
    priority = min((p for p in (prio.normalize_priority(m.priority) for m in down) if p),
                   key=prio.priority_rank, default=prio.severity_priority(sev))

    if final:
        ended = timestamp_label(storm.ended_at or datetime.now(timezone.utc), branding)
        head = (f"Alert storm over{where}: {total} {noun} {verb} between {since} and {ended}")
        extra = (f" — all {len(up)} recovered" if not down else f" — {len(up)} recovered, {len(down)} still affected")
        reason_line = (f"No new event of this kind for {storm.window_minutes} min, so the storm is over. "
                       + ("Every affected asset has recovered." if not down else
                          "Assets still affected are listed below; their recoveries are notified individually."))
    elif first:
        head = f"Alert storm{where}: {total} {noun} {verb} since {since}"
        extra = f" ({len(up)} already recovered)" if up else ""
        reason_line = (f"More than {storm.threshold} {noun} {verb} within {storm.window_minutes} min, so they are "
                       "reported together. Individual notifications for this storm are paused; updates follow "
                       "while it continues.")
    else:
        new = len(new_ids)
        head = (f"Alert storm update{where}: {total} {noun} affected since {since}"
                + (f" ({new} new)" if new else ""))
        extra = f" — {len(down)} still affected, {len(up)} recovered"
        reason_line = "The storm is still going on. Individual notifications for it remain paused."

    def link(path: str) -> str:
        return f"{base}{path}" if base and path else ""

    rows = []
    for m in members:
        aid = str(m.alert_id) if m.alert_id else None
        rows.append({
            "incident_ref": incident_ref(aid) if aid else "",
            "asset": m.asset_label or "",
            "condition": ("recovered" if _member_recovered(m) else
                          ("new since the last update" if aid in new_ids and not first else (m.message or verb))),
            "value": "" if m.held else "notified individually",
            "first_seen": (m.triggered_at or m.joined_at).isoformat() if (m.triggered_at or m.joined_at) else "",
            "recovered": _member_recovered(m),
            "alert_id": aid, "url": link(f"/alerts/{aid}") if aid else "",
            "severity": (m.severity or "warning"), "priority": m.priority,
        })
    body_lines = [f"{head}{extra}", "", reason_line, ""]
    if down:
        body_lines.append(f"Still affected ({len(down)}):")
        body_lines += [f"- {r['asset']} ({r['incident_ref']})" for r in rows if not r["recovered"]][:200]
    if up:
        body_lines += ["", f"Recovered ({len(up)}):"]
        body_lines += [f"- {r['asset']}" for r in rows if r["recovered"]][:200]
    if storm_url:
        body_lines += ["", f"Details: {storm_url}"]
    sms = (f"ZenPlus {head}{extra}. "
           + (f"Affected: {_names([m.asset_label for m in down])}. " if down else "")
           + (storm_url if storm_url else ""))
    anchor = next((str(i.alert_id) for i in items if i.alert_id),
                  next((str(m.alert_id) for m in members if m.alert_id), None))
    resolved = final and not down
    return {
        "subject": f"[{'RESOLVED' if resolved else sev.upper()}] [{priority}] {head}{extra}",
        "body": "\n".join(body_lines),
        "message": sms[:480],
        "html_body": ng.digest_email_html(
            rows, head=head, extra=extra, severity=sev, priority=priority, reason_line=reason_line,
            digest_url=storm_url, max_items=100, branding=branding, chip="STORM",
            footer=("One message per storm replaces the individual notifications of the assets listed "
                    "above. Configure under Escalation & On-call → Alert storms."),
            button="View storm", recovered_title="Recovered"),
        "rule_name": f"Alert storm: {FAMILY_TITLES.get(storm.family, storm.family)}",
        "hostname": storm.scope_label or "All monitored assets",
        "ip_address": "",
        "status": "RESOLVED" if resolved else f"ALERTING ({len(down)} affected)",
        "severity": sev,
        "is_recovery": resolved,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "action_url": storm_url,
        "alert_id": anchor,
        "delivery_event": f"storm:{d.id}",
        "delivery_kind": "digest",
        "groupable": False,
        "storm": True,
        "dedup_key": f"zenplus:storm:{storm.id}",
        "type": "storm",
        "storm_summary": {"id": str(storm.id), "family": storm.family, "scope": storm.scope_label,
                          "phase": "final" if final else ("first" if first else "update"),
                          "affected": len(down), "recovered": len(up), "total": total,
                          "assets": [{"asset": r["asset"], "recovered": r["recovered"],
                                      "incident": r["incident_ref"]} for r in rows]},
    }


async def release_one(db: AsyncSession, d, dispatch: Callable | None = None) -> str:
    """Send one combined storm notification (first, update or closing)."""
    from app.services import host_alert_service as has

    storm_id = storm_id_of(d.group_key)
    storm = (await db.execute(text("SELECT * FROM alert_storms WHERE id = CAST(:s AS uuid)"),
                              {"s": storm_id})).first() if storm_id else None
    items = await ng._items(db, d.id)
    if storm is None:
        await ng._finish(db, d.id, "cancelled", error="The alert storm no longer exists.")
        await db.commit()
        return "cancelled"
    final = str(d.group_key).endswith(":final")
    sent_before = (await db.execute(text(
        "SELECT count(*) FROM notification_digests WHERE group_key = :gk"
        " AND channel_id IS NOT DISTINCT FROM CAST(:cid AS uuid) AND recipient = :rcpt AND status = 'sent'"),
        {"gk": f"storm:{storm_id}", "cid": str(d.channel_id) if d.channel_id else None,
         "rcpt": d.recipient or ""})).scalar()
    members = await _members(db, storm_id)
    ctx = await storm_context(db, d, storm, members, items, final=final, first=not sent_before and not final)
    channel_ids: list = []
    if str(d.channel_id or "") == ng.DIRECT_CHANNEL_ID:
        kind, _, address = (d.recipient or "").partition(":")
        ctx["recipients"] = [{"kind": kind, "address": address, "user_id": None, "label": None}]
    else:
        channel_ids = [str(d.channel_id)] if d.channel_id else []
        if d.recipient:
            ctx["only_recipients"] = d.recipient.split(",")
    send = dispatch or has.dispatch_to_channels
    detail = {"digest_id": str(d.id), "storm_id": storm_id, "items": len(items)}
    try:
        n = await send(db, channel_ids, ctx)
    except Exception as exc:
        await db.rollback()
        logger.warning("storm digest %s: send failed: %s", d.id, exc)
        n, err = 0, f"send failed: {str(exc)[:200]}"
    else:
        err = None if n else ("No channel accepted the storm notification (channel disabled, no recipients "
                              "or no enabled gateway); see the delivery log.")
    if n:
        await ng._finish(db, d.id, "sent", sent=True)
        await ng._event_all(db, items, "notification_released",
                            reason="Reported in the alert storm summary", detail=detail)
        await db.commit()
        return "sent"
    settings = await ng.load_settings(db)
    return await ng._fail_or_retry(db, d, err, items, detail, interval=int(settings["group_interval_seconds"]))


# ── Read models ──────────────────────────────────────────────────────────────

def storm_payload(r, *, members: list | None = None) -> dict:
    out = {
        "id": str(r.id), "family": r.family, "family_label": FAMILY_TITLES.get(r.family, r.family),
        "scope_label": r.scope_label, "status": r.status, "threshold": r.threshold,
        "window_minutes": r.window_minutes, "started_at": r.started_at, "declared_at": r.declared_at,
        "last_event_at": r.last_event_at, "ended_at": r.ended_at,
        "member_count": int(getattr(r, "member_count", 0) or 0),
        "recovered_count": int(getattr(r, "recovered_count", 0) or 0),
    }
    if members is not None:
        out["members"] = [{"alert_id": str(m.alert_id) if m.alert_id else None, "asset": m.asset_label,
                           "held": bool(m.held), "joined_at": m.joined_at,
                           "recovered": _member_recovered(m), "status": m.alert_status,
                           "message": m.message} for m in members]
    return out
