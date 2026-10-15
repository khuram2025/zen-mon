"""Durable "flapping started" / "flapping stopped" notices for devices.

The status writers (the Go poller and the remote-sensor ingest) open and close
rows in ``device_flap_episodes``; they never post flap events to the alert
engine. This module runs on the escalation sweeper's 30 s cadence and turns
each episode into at most one start notice and one stop notice, so a notice
survives an API restart or a lost request (docs/device-status-policy.md).

* Start (``start_notice='pending'``): the target rules are the enabled device
  status rules — the same rule set and scope filters as the status path —
  that the episode actually holds (:func:`rule_held`): the trigger belongs to
  a flapping class (reachability: any/down, performance: any/degraded), the
  rule's metric gate can pass on that class's move (the seeded "Device Down",
  any + ping_status == 0, never fires on degraded), and the rule does not
  notify while flapping. Of those, the ones not snoozed for this device and
  inside their quiet-hours schedule are told. The notice goes ONCE to each
  channel in the union of their channels, never to a stateful incident
  channel (PagerDuty-type): a notice must not open an incident that nothing
  closes. The channels it went to are stored on the episode.
* Stop (episode ended, ``stop_notice='pending'``, start already settled): only
  to the channels the start notice reached, with the current status. It
  follows the start notice's fate as a recovery follows its trigger, so quiet
  hours are not applied again, and an episode whose start was not sent ends
  silently.
* A device in maintenance, or owned by an offline sensor, gets neither
  (``skipped``).
* An episode that had already ended when its start notice came due (the API
  was down, or a sensor's late batch opened and closed it) is history, not
  news: "notifications are held" would no longer be true, so it gets neither
  notice. The episode itself stays in the device's flapping history. Once a
  start notice was sent, the stop follows whatever ended the episode (stable,
  detection turned off, a reseed, polling handed over or stopped), so
  everyone told that notifications were held hears that they resumed.

Each notice sent (and each start notice a matching rule kept quiet) is
recorded as ONE informational alert row: ``rule_id`` NULL, severity info,
status resolved, ``metadata.flap_notice``. It is a notice, never an incident
(``alert_workflow.notice_sql``), never escalated, and a rule's cooldown never
matches it (cooldown is per ``rule_id``).

Settle, then dispatch: the episode's notice state and the notice row are
committed before anything is sent, so a crash mid-dispatch can lose a notice
but never repeat one. Episodes are claimed one at a time in ``started_at``
order with ``FOR UPDATE SKIP LOCKED``, so both uvicorn workers can sweep; a
claim locks the device row before the episode, in the writers' lock order. A
device's episodes settle strictly in order (start, stop, next start, ...): an
episode waits while an earlier one of the same device still has a pending
notice, so "stopped flapping" can never arrive after the next "is flapping".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import alert_engine as engine
from app.services import alert_phrasing as ap
from app.services import device_status_machine as dsm
from app.services.alert_schedule import get_configured_timezone, notifications_allowed
from app.services.tag_service import tag_set

logger = logging.getLogger("zenplus.device_flap_notice")

# Episodes handled per sweep and notice kind; the rest wait for the next sweep.
BATCH = 50

# Channels that keep incident state on the far side (trigger opens, resolve
# closes). A notice is not an incident, so they never receive one.
STATEFUL_CHANNEL_TYPES = frozenset({"pagerduty", "opsgenie"})

# Which rule triggers an episode class holds (and so which rules hear of it).
# A rule that triggers only on 'up' announces recoveries, which are never
# held, so a "notifications are held" notice would be false for it; the same
# goes for a rule that notifies while flapping and for one whose conditions
# cannot match the class's move (see rule_held).
_CLASS_TRIGGERS = {
    "reachability": frozenset({"any", "down"}),
    "performance": frozenset({"any", "degraded"}),
}

# The move whose triggers a flapping class holds: into down (reachability),
# into degraded (performance).
_CLASS_TARGET = {"reachability": "down", "performance": "degraded"}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_EPISODE_COLUMNS = (
    "e.id, e.device_id, e.started_at, e.ended_at, e.kind, e.start_changes,"
    " e.window_minutes, e.status_at_start, e.status_at_end, e.end_reason,"
    " e.start_notice, e.stop_notice, e.notice_channels"
)

# An earlier episode of the same device that is not fully settled (possibly
# claimed by the other worker right now: the subquery reads committed state
# and takes no lock) holds this one back, so one device's notices never pass
# each other. It is ended (one open episode per device), so it settles on its
# own.
_NO_EARLIER_PENDING = """
          AND NOT EXISTS (
              SELECT 1 FROM device_flap_episodes p
              WHERE p.device_id = e.device_id
                AND (p.started_at, p.id) < (e.started_at, e.id)
                AND (p.start_notice = 'pending' OR p.stop_notice = 'pending'))
"""

# Which episodes owe a notice. Both imply the partial index predicate
# (start_notice = 'pending' OR stop_notice = 'pending'), so the work queue is
# an index scan in started_at order.
_PENDING = {
    "start": "e.start_notice = 'pending'",
    "stop": "e.stop_notice = 'pending' AND e.ended_at IS NOT NULL AND e.start_notice <> 'pending'",
}

# The next candidate, unlocked. ``skip`` holds the episodes this sweep already
# failed on or found claimed by the other worker.
_NEXT_SQL = {
    flap_event: f"""
        SELECT e.id, e.device_id
        FROM device_flap_episodes e
        WHERE {pending}
          AND NOT (e.id = ANY(CAST(:skip AS uuid[]))){_NO_EARLIER_PENDING}
        ORDER BY e.started_at, e.id
        LIMIT 1
    """
    for flap_event, pending in _PENDING.items()
}

# Lock order of every status writer is devices -> device_status_state ->
# device_flap_episodes, and the notice row's foreign key needs the device row
# (KEY SHARE) anyway. Taking it after the episode would deadlock with a writer
# that holds the device and waits to close this very episode, so the claim
# takes the device first. KEY SHARE passes the writers' FOR NO KEY UPDATE and
# waits out a FOR UPDATE while holding nothing else.
_LOCK_DEVICE_SQL = "SELECT 1 FROM devices WHERE id = :id FOR KEY SHARE"

# The claim proper: the candidate again, rechecked under its row lock.
_CLAIM_SQL = {
    flap_event: f"""
        SELECT {_EPISODE_COLUMNS}
        FROM device_flap_episodes e
        WHERE e.id = :id AND {pending}{_NO_EARLIER_PENDING}
        FOR UPDATE SKIP LOCKED
    """
    for flap_event, pending in _PENDING.items()
}

_SETTLE_SQL = {
    "start": """
        UPDATE device_flap_episodes
        SET start_notice = :state, notice_channels = CAST(:channels AS uuid[])
        WHERE id = :id AND start_notice = 'pending'
    """,
    "stop": """
        UPDATE device_flap_episodes
        SET stop_notice = :state
        WHERE id = :id AND stop_notice = 'pending'
    """,
}


def rule_relevant(trigger_on: str | None, kind: str | None) -> bool:
    """Does an episode of ``kind`` hold this trigger's notifications?

    reachability: any/down; performance: any/degraded; both: either.
    """
    trigger = trigger_on or "any"
    return any(trigger in _CLASS_TRIGGERS.get(c or "", ()) for c in _classes(kind))


def _classes(kind: str | None) -> tuple:
    return ("reachability", "performance") if kind == "both" else (kind,)


def rule_held(rule, kind: str | None) -> bool:
    """Would an episode of ``kind`` hold this rule's status notifications?

    For at least one flapping class: the rule's trigger belongs to the class
    and its metric gate can pass on the class's move, exactly as the status
    path evaluates it (alert_engine._could_fire_into). A rule that notifies
    while flapping is never held. Anything else would be told "notifications
    are held" about notifications the flap never touches.
    """
    if getattr(rule, "notify_while_flapping", False):
        return False
    return any(
        c in _CLASS_TARGET and rule_relevant(rule.trigger_on, c)
        and engine._could_fire_into(rule, _CLASS_TARGET[c])
        for c in _classes(kind))


def union_channels(rules) -> list[str]:
    """Every channel id of ``rules`` once, in rule order (ids that are not
    UUIDs cannot name a channel and are dropped)."""
    out: list[str] = []
    for rule in rules:
        chans = getattr(rule, "notify_channels", None) or []
        if isinstance(chans, str):
            try:
                chans = json.loads(chans)
            except ValueError:
                chans = []
        for ch in chans if isinstance(chans, list) else []:
            cid = str(ch).lower()
            if _UUID_RE.match(cid) and cid not in out:
                out.append(cid)
    return out


@dataclass
class _Sweep:
    """What every episode of one sweep shares."""
    rules: list
    policy: dsm.Policy
    tz: str
    action_base: str


@dataclass
class _Plan:
    """What to settle for one episode, and what (if anything) to send."""
    state: str                          # 'sent' | 'skipped'
    reason: str = ""                    # why skipped (logs, tests)
    channels: list[str] = field(default_factory=list)
    notice: dict[str, Any] | None = None  # the alert row to record
    dispatch: dict[str, Any] | None = None  # dispatch_to_channels ctx (sans alert_id)


_SUPPORTED = False
_warned_missing = False


async def _supported(db: AsyncSession) -> bool:
    """Whether migrate-161 has created device_flap_episodes. A positive answer
    is cached; a negative one is asked again on the next sweep."""
    global _SUPPORTED, _warned_missing
    if not _SUPPORTED:
        _SUPPORTED = bool((await db.execute(
            text("SELECT to_regclass('public.device_flap_episodes') IS NOT NULL"))).scalar())
        await db.rollback()
        if not _SUPPORTED and not _warned_missing:
            logger.warning("device flap notices disabled until "
                           "migrate-161-device-status-confirmation.sql runs")
            _warned_missing = True
    return _SUPPORTED


async def deliver_pending(db: AsyncSession) -> dict[str, int]:
    """Deliver pending start notices, then pending stop notices."""
    out = {"started": 0, "stopped": 0, "skipped": 0, "failed": 0}
    if not await _supported(db):
        return out
    sweep: _Sweep | None = None
    for flap_event in ("start", "stop"):
        skip: list[str] = []
        for _ in range(BATCH):
            episode = await _claim(db, flap_event, skip)
            if episode is None:
                await db.rollback()
                break
            if sweep is None:
                # Rules, policy and timezone are the sweep's, not the
                # episode's: if they cannot be read the whole sweep stops
                # (the caller logs it) and the claim is released unsettled.
                sweep = await _load_sweep(db)
            try:
                plan = await (_plan_start if flap_event == "start" else _plan_stop)(db, episode, sweep)
                notice_id = await _settle(db, episode, flap_event, plan)
                # Settled before anything is sent: a crash from here on loses
                # this notice, it never sends it twice.
                await db.commit()
            except Exception:
                await db.rollback()
                skip.append(str(episode.id))
                out["failed"] += 1
                logger.exception("flap %s notice failed for episode %s", flap_event, episode.id)
                continue
            if plan.state != "sent":
                out["skipped"] += 1
                continue
            out["started" if flap_event == "start" else "stopped"] += 1
            try:
                await _dispatch(db, plan, notice_id)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("flap %s notice dispatch failed for episode %s",
                                 flap_event, episode.id)
    return out


async def _claim(db: AsyncSession, flap_event: str, skip: list[str]):
    """The next episode owing a ``flap_event`` notice, with its device row
    and then itself locked; None once the queue is empty."""
    while True:
        candidate = (await db.execute(text(_NEXT_SQL[flap_event]), {"skip": skip})).first()
        if candidate is None:
            return None
        await db.execute(text(_LOCK_DEVICE_SQL), {"id": candidate.device_id})
        episode = (await db.execute(text(_CLAIM_SQL[flap_event]), {"id": candidate.id})).first()
        if episode is not None:
            return episode
        # The other worker holds it, or it settled meanwhile: release the
        # device and move on.
        await db.rollback()
        skip.append(str(candidate.id))


async def _load_sweep(db: AsyncSession) -> _Sweep:
    return _Sweep(
        rules=[r for r in await engine._load_device_rules(db) if engine._status_path_rule(r)],
        policy=await engine._status_policy(db),
        tz=await get_configured_timezone(db),
        action_base=await engine._dashboard_url(db, "/devices"),
    )


async def _device(db: AsyncSession, device_id):
    return (await db.execute(text("""
        SELECT d.id, d.hostname, host(d.ip_address) AS ip, d.status, d.status_changed_at,
               d.device_type, d.group_id, d.location, d.tags
        FROM devices d WHERE d.id = :id
    """), {"id": device_id})).first()


async def _quiet_reason(db: AsyncSession, device) -> str:
    """'maintenance' / 'sensor_offline' when the device must hear nothing."""
    did = str(device.id)
    if await engine._device_in_maintenance(db, did):
        return "maintenance"
    if await engine._offline_sensor_owner(db, did):
        return "sensor_offline"
    return ""


async def _snoozed_rule_ids(db: AsyncSession, device_id) -> set[str]:
    """Rules with an active snooze on this device (the dedupe the status path
    checks: 'rule:<id>')."""
    rows = (await db.execute(text("""
        SELECT dedupe FROM alert_silences
        WHERE device_id = :did AND dedupe LIKE 'rule:%'
          AND (until IS NULL OR until > NOW())
    """), {"did": device_id})).fetchall()
    return {r[0].split(":", 1)[1].lower() for r in rows if r[0]}


async def _deliverable(db: AsyncSession, channel_ids: list[str]) -> list[str]:
    """``channel_ids`` minus stateful incident channels and deleted channels,
    order kept. Disabled channels stay: the delivery log records the skip."""
    if not channel_ids:
        return []
    types = {r.id: r.type for r in (await db.execute(text(
        "SELECT id::text AS id, type FROM notification_channels"
        " WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": channel_ids})).fetchall()}
    return [c for c in channel_ids if c in types and types[c] not in STATEFUL_CHANNEL_TYPES]


def _host(device) -> tuple[str, str]:
    ip = device.ip or ""
    return (device.hostname or ip or "device"), ip


def _since_text(ts, tz_name: str) -> str:
    if not ts:
        return "an unknown time"
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(tz)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzname() or tz_name}"


def _render(flap_event: str, variables: dict) -> dict:
    templates = ap.flap_notice_templates(flap_event)
    return {k: engine._render(v, variables) for k, v in templates.items()}


async def _plan_start(db: AsyncSession, episode, sweep: _Sweep) -> _Plan:
    if episode.ended_at is not None:
        # Over before anyone could be told; its stop then ends silently too.
        return _Plan("skipped", "ended")
    device = await _device(db, episode.device_id)
    if device is None:
        return _Plan("skipped", "device_missing")
    quiet = await _quiet_reason(db, device)
    if quiet:
        return _Plan("skipped", quiet)

    did = str(device.id)
    candidates = [
        r for r in sweep.rules
        if rule_held(r, episode.kind)
        and engine._rule_in_device_scope(
            r, device_id=did, group_id=str(device.group_id) if device.group_id else None,
            device_type=device.device_type, location=device.location,
            device_tags=tag_set(device.tags))
    ]
    if not candidates:
        return _Plan("skipped", "no_rules")

    snoozed = await _snoozed_rule_ids(db, did)
    targets, quiet_rules, snoozed_rules = [], [], []
    for rule in candidates:
        rid = str(rule.id).lower()
        if rid in snoozed:
            snoozed_rules.append(rid)
        elif not notifications_allowed(rule.schedule_start, rule.schedule_end,
                                       getattr(rule, "schedule_days", None), sweep.tz):
            quiet_rules.append(rid)
        else:
            targets.append(rule)
    channels = await _deliverable(db, union_channels(targets))

    policy = sweep.policy
    hostname, ip = _host(device)
    changes = episode.start_changes or policy.flap_start_changes
    window = episode.window_minutes or policy.flap_window_minutes
    kind_label = ap.flap_kind_label(episode.kind)
    variables = {
        "hostname": hostname, "ip": ip, "ip_address": ip,
        "changes": changes, "window": window, "kind_label": kind_label,
        "stable": policy.flap_stable_minutes,
        # What this kind holds and leaves alone: a latency/loss episode never
        # holds a Down.
        **ap.flap_held_labels(episode.kind),
    }
    text_ = _render("start", variables)
    sent = bool(channels)
    notice = {
        "message": (f"{ap.host_label(hostname, ip)} is flapping: {changes} status changes "
                    f"in {window} minutes ({kind_label})"),
        "metadata": {
            "flap_notice": True, "flap_event": "start", "episode_id": str(episode.id),
            "kind": episode.kind, "changes": changes, "window_minutes": window,
            "stable_minutes": policy.flap_stable_minutes,
            "flapping_since": _iso(episode.started_at),
            "notified": sent,
            "target_rule_ids": [str(r.id) for r in targets],
            "quiet_hours_rule_ids": quiet_rules,
            "snoozed_rule_ids": snoozed_rules,
            "channels": channels,
        },
    }
    dispatch = {
        "subject": text_["email_subject"],
        "body": text_["email_body"],
        "message": text_["sms_template"],
        "hostname": hostname,
        "ip_address": ip,
        "status": "FLAPPING",
        "severity": "info",
        "resolved": False,
        "is_recovery": False,
        "rule_name": "Device flapping",
        "notice": ap.flap_held_notice(episode.kind, policy.flap_stable_minutes),
        "details": [
            ("Status changes", f"{changes} in {window} minutes"),
            ("Flapping", kind_label),
            ("Flapping since", _since_text(episode.started_at, sweep.tz)),
            ("Status now", (device.status or "unknown").upper()),
        ],
        "action_url": _device_url(sweep, did),
        "device_id": did,
        "episode_id": str(episode.id),
        "flap_kind": episode.kind,
        "event_type": "flap_start",
        "delivery_kind": "flap_start",
    }
    return _Plan("sent" if sent else "skipped", "" if sent else "no_channels",
                 channels=channels, notice=notice, dispatch=dispatch if sent else None)


async def _plan_stop(db: AsyncSession, episode, sweep: _Sweep) -> _Plan:
    # Follows the start notice's fate: nobody was told it started.
    reached = [str(c).lower() for c in (episode.notice_channels or [])]
    if episode.start_notice != "sent" or not reached:
        return _Plan("skipped", "start_not_sent")
    device = await _device(db, episode.device_id)
    if device is None:
        return _Plan("skipped", "device_missing")
    quiet = await _quiet_reason(db, device)
    if quiet:
        return _Plan("skipped", quiet)
    channels = await _deliverable(db, reached)
    if not channels:
        return _Plan("skipped", "no_channels")

    did = str(device.id)
    hostname, ip = _host(device)
    status = (device.status or "unknown").lower()
    since = _since_text(device.status_changed_at, sweep.tz)
    reason_label = ap.flap_end_reason_label(episode.end_reason)
    variables = {
        "hostname": hostname, "ip": ip, "ip_address": ip,
        "STATUS": status.upper(), "status": status.upper(), "since": since,
        "end_reason_label": reason_label,
    }
    text_ = _render("stop", variables)
    lasted = ap.duration_between(episode.started_at, episode.ended_at)
    notice = {
        "message": (f"{ap.host_label(hostname, ip)} stopped flapping ({reason_label}) "
                    f"— {status.upper()}"),
        "metadata": {
            "flap_notice": True, "flap_event": "stop", "episode_id": str(episode.id),
            "kind": episode.kind, "changes": episode.start_changes,
            "window_minutes": episode.window_minutes,
            "end_reason": episode.end_reason,
            "device_status": status,
            "status_since": _iso(device.status_changed_at),
            "flapping_since": _iso(episode.started_at),
            "flapping_ended_at": _iso(episode.ended_at),
            "notified": True,
            "channels": channels,
        },
    }
    # Back to UP reads as an all-clear; a device that settled DOWN or DEGRADED
    # is not one (its own status alert carries that page).
    resolved = status == "up"
    dispatch = {
        "subject": text_["email_subject"],
        "body": text_["email_body"],
        "message": text_["sms_template"],
        "hostname": hostname,
        "ip_address": ip,
        "status": status.upper(),
        "severity": "info",
        "resolved": resolved,
        "is_recovery": resolved,
        "rule_name": "Device stopped flapping",
        "notice": "Status notifications for this device have resumed.",
        "details": [
            ("Status now", f"{status.upper()} since {since}"),
            ("Flapping ended", reason_label),
            ("Flapped for", lasted),
            ("Flapping", ap.flap_kind_label(episode.kind)),
        ],
        "action_url": _device_url(sweep, did),
        "device_id": did,
        "episode_id": str(episode.id),
        "flap_kind": episode.kind,
        "end_reason": episode.end_reason,
        "event_type": "flap_stop",
        "delivery_kind": "flap_stop",
    }
    return _Plan("sent", channels=channels, notice=notice, dispatch=dispatch)


def _device_url(sweep: _Sweep, device_id: str) -> str:
    return f"{sweep.action_base}/{device_id}" if sweep.action_base else ""


def _iso(ts) -> str | None:
    if not ts:
        return None
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


async def _settle(db: AsyncSession, episode, flap_event: str, plan: _Plan):
    """Record the episode's notice state and the notice row; returns the
    notice row id (None when nothing is recorded)."""
    params: dict[str, Any] = {"id": episode.id, "state": plan.state}
    if flap_event == "start":
        params["channels"] = plan.channels
    await db.execute(text(_SETTLE_SQL[flap_event]), params)
    if plan.notice is None:
        return None
    now = datetime.now(timezone.utc)
    return (await db.execute(text("""
        INSERT INTO alerts (device_id, rule_id, status, severity, message,
                            triggered_at, resolved_at, metadata)
        VALUES (:device_id, NULL, 'resolved', 'info', :message,
                :now, :now, CAST(:metadata AS jsonb))
        RETURNING id
    """), {"device_id": episode.device_id, "message": plan.notice["message"], "now": now,
           "metadata": json.dumps(plan.notice["metadata"])})).scalar()


async def _dispatch(db: AsyncSession, plan: _Plan, notice_id) -> int:
    from app.services.host_alert_service import dispatch_to_channels

    ctx = dict(plan.dispatch or {})
    ctx["alert_id"] = str(notice_id) if notice_id else None
    ctx["triggered_at"] = datetime.now(timezone.utc).isoformat()
    return await dispatch_to_channels(db, plan.channels, ctx)
