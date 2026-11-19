"""Monitoring gap alerts (alerting plan G-2; docs/monitoring-gap-alerts.md).

Two rule metrics that page when monitoring itself stops delivering while the
asset is plainly alive, evaluated every ``EVAL_INTERVAL_S`` in one API worker
at a time (``alert_worker_lock``):

``snmp_unreachable``
    Minutes the controller poller's SNMP polls of a device have gone
    unanswered (``device_snmp_poll_state.failing_since``, migrate-202) while
    the device answers ping (status up or degraded). 0 while polls are
    answered. A device that does not answer ping is the Device Down rules'
    business, so it has no reading; nor has a device whose poll state is
    stale (not polled for ``max(3 x snmp_poll_interval, 10 min)``, e.g. moved
    to a sensor) or that has never been polled.
``agent_no_metrics``
    Minutes since an agent server's last host metric batch
    (``agents.last_metric_at``) while its heartbeat is current (younger than
    ``server_availability.DOWN_AFTER_S``). 0 while a batch arrived within
    ``METRIC_FRESH_S``. A silent heartbeat is the availability rules'
    business (no reading); an agent that never delivered host metrics (an
    APM-only install) has no reading either.

A rule takes one condition ``<metric> > N`` or ``>= N`` (N in minutes, the
gap that pages). Lifecycle, as for the other metric alerts in AGENTS.md: one
incident per rule + asset; the gap must reach N, then hold for
``min_duration``; an open incident resolves only once the gap has closed
(reading 0) and stayed closed for ``clear_duration`` (NULL = 300 s); a
missing reading never raises or resolves. Every send goes through
``dispatch_to_channels`` with the alert id and delivery event; a recovery is
sent only when the trigger was and the rule asks for one.

Devices in maintenance (manual or a window), servers in a maintenance window
or whose linked ping device is in maintenance, and assets a shared rule is excluded for
(``alert_rule_exclusions``) are skipped; an open incident stays as it is.
Hold state lives in ``service_metric_alert_state`` (migrate-199), keyed
``gap:<kind>:<id>``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_funnel as funnel
from app.services import alert_notify_state as ns

logger = logging.getLogger("zenplus.monitoring_gap_alerts")

SNMP_UNREACHABLE = "snmp_unreachable"
AGENT_NO_METRICS = "agent_no_metrics"
METRICS = frozenset({SNMP_UNREACHABLE, AGENT_NO_METRICS})
DEVICE_METRICS = frozenset({SNMP_UNREACHABLE})
SERVER_METRICS = frozenset({AGENT_NO_METRICS})

EVAL_INTERVAL_S = 60
DEFAULT_CLEAR_S = 300
SNMP_STALE_FLOOR_S = 600
METRIC_FRESH_S = 180
MAX_MINUTES = 10080          # a week: the largest gap a rule may wait for

_OPS = {
    ">": lambda a, b: a > b, "gt": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "gte": lambda a, b: a >= b,
}
OPERATORS = tuple(_OPS)


# ─── Conditions and lifecycle ────────────────────────────────────────────────

def rule_condition(rule) -> dict:
    conds = getattr(rule, "conditions", None)
    if isinstance(conds, str):
        try:
            conds = json.loads(conds)
        except ValueError:
            conds = None
    c = dict(conds[0]) if conds else {"metric": rule.metric, "operator": rule.operator,
                                       "threshold": rule.threshold}
    c["threshold"] = float(c.get("threshold") or 0)
    c["operator"] = (c.get("operator") or ">=").strip()
    return c


def breaching(value: float, condition: dict) -> bool:
    fn = _OPS.get(condition.get("operator") or ">=")
    return bool(fn and fn(float(value), float(condition["threshold"])))


def clear_hold_seconds(clear_duration) -> int:
    return DEFAULT_CLEAR_S if clear_duration is None else max(0, int(clear_duration))


@dataclass
class Decision:
    action: str                  # "raise" | "resolve" | "hold" | "none"
    breach_since: Optional[datetime]
    clear_since: Optional[datetime]


def decide(*, value: Optional[float], condition: dict, open_incident: bool,
           breach_since: Optional[datetime], clear_since: Optional[datetime],
           hold_s: int, clear_hold_s: int, now: datetime) -> Decision:
    """One lifecycle step for one rule + asset.

    Raise once the gap reached the limit and held ``hold_s``; resolve only
    once the gap closed (0) and stayed closed ``clear_hold_s``. A gap that is
    open but still below the limit neither raises nor clears. A missing
    reading does nothing and restarts any hold."""
    if value is None:
        return Decision("none", None, None)
    if not open_incident:
        if breaching(value, condition):
            since = breach_since or now
            if (now - since).total_seconds() >= hold_s:
                return Decision("raise", None, None)
            return Decision("hold", since, None)
        return Decision("none", None, None)
    if value > 0:
        return Decision("none", None, None)
    since = clear_since or now
    if (now - since).total_seconds() >= clear_hold_s:
        return Decision("resolve", None, None)
    return Decision("hold", None, since)


# ─── Readings ────────────────────────────────────────────────────────────────

def _aware(ts) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def snmp_stale_s(interval) -> int:
    return max(3 * int(interval or 60), SNMP_STALE_FLOOR_S)


def snmp_reading(dev: dict, now: datetime) -> Optional[float]:
    """Minutes SNMP polls have gone unanswered (0 = answered), or None."""
    if dev.get("status") not in ("up", "degraded"):
        return None
    attempt = _aware(dev.get("last_attempt_at"))
    if attempt is None or (now - attempt).total_seconds() > snmp_stale_s(dev.get("snmp_poll_interval")):
        return None
    since = _aware(dev.get("failing_since"))
    if since is None:
        return 0.0
    return max(0.0, (now - since).total_seconds() / 60.0)


def agent_reading(heartbeat_at, last_metric_at, now: datetime) -> Optional[float]:
    """Minutes since the agent's last host metric batch (0 = current), or None."""
    from app.services.server_availability import DOWN_AFTER_S
    hb = _aware(heartbeat_at)
    if hb is None or (now - hb).total_seconds() >= DOWN_AFTER_S:
        return None
    last = _aware(last_metric_at)
    if last is None:
        return None
    age = (now - last).total_seconds()
    if age < METRIC_FRESH_S:
        return 0.0
    return age / 60.0


def gap_text(minutes: float) -> str:
    """"15 minutes", "1 minute", "3 hours", "2 days"."""
    m = int(max(0, minutes))
    if m < 120:
        return f"{m} minute{'s' if m != 1 else ''}"
    h = m // 60
    if h < 48:
        return f"{h} hours"
    return f"{h // 24} days"


@dataclass
class Reading:
    key: str                         # "gap:device:<id>" | "gap:server:<id>"
    value: Optional[float]
    asset: str                       # "Switch core-1 (10.0.0.1)" / "Server web-01 (10.0.0.5)"
    hostname: str = ""
    ip: str = ""
    device_id: Optional[str] = None
    server_id: Optional[str] = None
    context: dict = field(default_factory=dict)


def sentence(metric: str, r: Reading, *, recovery: bool = False) -> str:
    if metric == SNMP_UNREACHABLE:
        if recovery:
            return f"SNMP polling of {r.asset} is answered again"
        return (f"SNMP polling of {r.asset} has failed for {gap_text(r.value or 0)} while the "
                f"device answers ping: check the SNMP credentials or ACL")
    if recovery:
        return f"The agent on {r.asset} is sending metrics again"
    return (f"The agent on {r.asset} has sent no metrics for {gap_text(r.value or 0)} although it "
            f"still sends heartbeats: check the agent's Server module and its log")


# ─── Assets ──────────────────────────────────────────────────────────────────

async def load_snmp_devices(db: AsyncSession) -> dict[str, dict]:
    """SNMP-polled devices that have a recorded poll state."""
    rows = (await db.execute(text(
        "SELECT d.id::text, d.hostname, host(d.ip_address), d.device_type, d.location, d.group_id::text, "
        "d.tags, d.profile_id::text, d.status, d.snmp_poll_interval, ps.last_attempt_at, ps.failing_since, "
        "ps.last_ok_at, ps.last_error, ps.consecutive_failures "
        "FROM devices d JOIN device_snmp_poll_state ps ON ps.device_id = d.id "
        "WHERE d.snmp_enabled = true AND COALESCE(d.poll_mode, 'direct') <> 'via_controller'"))).all()
    from app.services.device_groups import membership
    from app.services.tag_service import tag_set
    groups = await membership.load(db, "device") if rows else None
    out = {}
    for r in rows:
        out[r[0]] = {"hostname": r[1] or "device", "ip": r[2] or "", "device_type": r[3], "location": r[4],
                     "group_id": r[5], "tags": tag_set(r[6]), "profile_id": r[7], "status": r[8],
                     "snmp_poll_interval": r[9], "last_attempt_at": r[10], "failing_since": r[11],
                     "last_ok_at": r[12], "last_error": r[13] or "", "consecutive_failures": r[14] or 0,
                     "groups": (groups.get(r[0], set()) if groups is not None else None)}
    return out


async def load_agent_servers(db: AsyncSession) -> dict[str, dict]:
    """Agent servers with their heartbeat, last host metric batch and ping device state."""
    from app.services import server_availability as sa
    signals = await sa.load_signals(db)
    if not signals:
        return {}
    rows = (await db.execute(text(
        "SELECT s.id::text, host(s.primary_ip), max(a.last_metric_at) "
        "FROM servers s JOIN agents a ON a.server_id = s.id AND a.status <> 'disabled' "
        "WHERE s.id = ANY(CAST(:ids AS uuid[])) GROUP BY s.id, s.primary_ip"),
        {"ids": list(signals)})).all()
    from app.services.device_groups import membership
    groups = await membership.load(db, "server")
    out = {}
    for sid, ip, last_metric in rows:
        sig = signals[sid]
        out[sid] = {"hostname": sig.hostname, "ip": ip or "", "tags": sig.tags,
                    "heartbeat_at": sig.heartbeat_at, "last_metric_at": last_metric,
                    "ping": sig.ping, "maintenance": sig.maintenance,
                    "groups": (groups.get(sid, set()) if groups is not None else None)}
    return out


def device_rule_covers(rule, did: str, dev: dict, family=None) -> bool:
    from app.services.network_alert_service import _device_in_scope
    return _device_in_scope(rule, did, dev, family)


def server_rule_covers(rule, sid: str, srv: dict) -> bool:
    """All agent servers, narrowed by the rule's server, group and tag."""
    from app.services.device_groups.membership import in_group
    if getattr(rule, "server_id", None) and str(rule.server_id) != sid:
        return False
    if getattr(rule, "group_id", None) and not in_group(rule.group_id, srv.get("groups"), None):
        return False
    tag = (getattr(rule, "scope_tag", None) or "").strip().lower()
    return not tag or tag in srv.get("tags", set())


def device_asset(dev: dict) -> str:
    from app.services import alert_phrasing as ap
    return ap.asset_label(dev["hostname"], dev.get("ip"), ap.asset_type_label(dev.get("device_type"), "Device"))


def server_asset(srv: dict) -> str:
    from app.services import alert_phrasing as ap
    return ap.asset_label(srv["hostname"], srv.get("ip"), "Server")


# ─── Alert rows ──────────────────────────────────────────────────────────────

async def _open_alert(db: AsyncSession, rule_id, key: str):
    row = (await db.execute(text(
        "SELECT id, triggered_at FROM alerts WHERE rule_id = :rid AND metadata->>'gap_key' = :k "
        "AND status IN ('active','acknowledged') ORDER BY triggered_at DESC LIMIT 1"),
        {"rid": str(rule_id), "k": key})).first()
    return (row[0], row[1]) if row else None


async def _load_state(db: AsyncSession, rule_id, key: str):
    row = (await db.execute(text(
        "SELECT breach_since, clear_since FROM service_metric_alert_state "
        "WHERE rule_id = :rid AND asset_key = :k"), {"rid": str(rule_id), "k": key})).first()
    return (_aware(row[0]), _aware(row[1])) if row else (None, None)


async def _save_state(db: AsyncSession, rule_id, key: str, breach_since, clear_since,
                      value, now: datetime) -> None:
    await db.execute(text(
        "INSERT INTO service_metric_alert_state "
        "(rule_id, asset_key, breach_since, clear_since, last_value, last_seen_at, updated_at) "
        "VALUES (:rid, :k, :b, :c, :v, :now, :now) "
        "ON CONFLICT (rule_id, asset_key) DO UPDATE SET breach_since = EXCLUDED.breach_since, "
        "clear_since = EXCLUDED.clear_since, last_value = EXCLUDED.last_value, "
        "last_seen_at = EXCLUDED.last_seen_at, updated_at = EXCLUDED.updated_at"),
        {"rid": str(rule_id), "k": key, "b": breach_since, "c": clear_since, "v": value, "now": now})


def _metadata(rule, metric: str, r: Reading, condition: dict) -> dict:
    return {"rule_id": str(rule.id), "metric": metric, "gap_key": r.key,
            "gap_minutes": None if r.value is None else round(float(r.value), 1),
            "threshold": condition["threshold"], "data_state": "current", **r.context}


async def _raise(db: AsyncSession, rule, metric: str, r: Reading, condition: dict,
                 message: str, now: datetime):
    meta = {**_metadata(rule, metric, r, condition), **ns.sending_metadata()}
    p_col, p_val, p_params = funnel.priority_insert(
        await funnel.priority_supported(db), getattr(rule, "priority", None), rule.severity or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, server_id, rule_id, status, severity, message, "
        f"triggered_at, metadata{p_col}) VALUES (:did, :sid, :rid, 'active', :sev, :msg, :ts, "
        f"CAST(:meta AS jsonb){p_val}) RETURNING id"),
        {"did": r.device_id, "sid": r.server_id, "rid": str(rule.id),
         "sev": rule.severity or "warning", "msg": message, "ts": now,
         "meta": json.dumps(meta, default=str), **p_params})).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id, now: datetime, extra: dict) -> int:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"),
        {"ts": now, "id": alert_id, "m": json.dumps({
            "resolved_by": "monitoring_gap_evaluator", "resolved_at": now.isoformat(), **extra},
            default=str)})
    return res.rowcount or 0


async def _trigger_allowed(db: AsyncSession, rule, key: str, now: datetime) -> bool:
    """Outside the rule's cooldown for this asset and inside its schedule window."""
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    cooldown = int(getattr(rule, "cooldown", 0) or 0)
    if cooldown > 0:
        prior = (await db.execute(text(
            "SELECT 1 FROM alerts WHERE rule_id = :rid AND metadata->>'gap_key' = :k "
            "AND metadata->>'notified' = 'true' AND triggered_at > :since LIMIT 1"),
            {"rid": str(rule.id), "k": key, "since": now - timedelta(seconds=cooldown)})).first()
        if prior:
            return False
    tz = await get_configured_timezone(db)
    return notifications_allowed(getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
                                 getattr(rule, "schedule_days", None), tz)


def _render(template: str, variables: dict) -> str:
    for k, v in variables.items():
        template = template.replace("{" + k + "}", "" if v is None else str(v))
    return template


async def _notify(db: AsyncSession, rule, metric: str, r: Reading, condition: dict, *,
                  event_sentence: str, alert_id, first_seen, now: datetime,
                  recovery: bool = False, checked: bool = False) -> bool:
    """Send one trigger or recovery through dispatch_to_channels."""
    from app.services import alert_phrasing as ap
    from app.services.host_alert_service import dispatch_to_channels
    if not rule.notify_channels:
        return False
    if not recovery and not checked and not await _trigger_allowed(db, rule, r.key, now):
        return False
    sev = rule.severity or "warning"
    duration = ap.duration_between(first_seen, now) if recovery else ""
    dur_sentence = f" Active for {duration}." if duration else ""
    incident = ap.incident_context(alert_id=alert_id, first_seen=first_seen, now=now,
                                   lifecycle="recovered" if recovery else "new")
    noun = "SNMP polling gap" if metric == SNMP_UNREACHABLE else "Agent metrics gap"
    reading = "answered" if recovery and metric == SNMP_UNREACHABLE else (
        "reporting" if recovery else gap_text(r.value or 0))
    limit = gap_text(condition["threshold"])
    variables = {
        "severity": sev.upper(), "status": "RESOLVED" if recovery else "ALERT",
        "rule_name": rule.name or "Alert", "host_suffix": f" — {r.asset}",
        "hostname": r.hostname or r.asset, "ip_address": r.ip, "asset": r.asset,
        "reading": reading, "threshold": limit, "metric": noun,
        "operator": condition.get("operator") or "", "event_sentence": event_sentence,
        "duration": duration, "duration_sentence": dur_sentence, "duration_suffix": dur_sentence,
        "timestamp": now.strftime("%H:%M UTC on %d %b %Y"), **incident,
    }
    # {site}, {group}, {site_suffix} of the asset (notification_context).
    from app.services import notification_context
    variables.update(await notification_context.asset_place(db, {"alert_id": alert_id}))
    prefix = "recovery_" if recovery else ""
    subject = _render(ap.effective_template(
        getattr(rule, prefix + "email_subject", None),
        ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if recovery else ap.DEFAULT_EMAIL_SUBJECT), variables)
    body = _render(ap.effective_template(
        getattr(rule, prefix + "email_body", None),
        ap.DEFAULT_RECOVERY_EMAIL_BODY if recovery else ap.DEFAULT_EMAIL_BODY), variables)
    sms = _render(ap.effective_template(
        getattr(rule, "recovery_sms_template" if recovery else "sms_template", None),
        ap.DEFAULT_RECOVERY_SMS if recovery else ap.DEFAULT_SMS), variables)
    details = [("Alert rule", rule.name), ("Asset", r.asset)]
    if r.context.get("last_error") and not recovery:
        details.append(("Last SNMP error", r.context["last_error"]))
    details += [("Pages after", limit), ("Active for", duration or None)]
    sent = await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": subject, "body": body, "message": sms,
        "hostname": r.hostname or r.asset, "ip_address": r.ip,
        "status": "RESOLVED" if recovery else "ALERT", "severity": sev, "resolved": recovery,
        "rule_name": rule.name, "metric": metric, "event_family": "monitoring_gap",
        "headline_metric": {"label": noun, "value": reading,
                            "secondary_label": "Pages after", "secondary_value": limit},
        "details": details,
        "triggered_at": now.isoformat(), "rule_id": str(rule.id),
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": "recovery" if recovery else "trigger", "is_recovery": recovery,
        **funnel.notification_fields(sev, getattr(rule, "priority", None), trigger=not recovery),
        **incident,
    })
    return bool(sent)


# ─── One rule + asset ────────────────────────────────────────────────────────

async def apply_reading(db: AsyncSession, rule, metric: str, r: Reading, condition: dict,
                        now: datetime, counts: dict) -> None:
    """Run the lifecycle for one reading, committing as it goes."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"monitoring_gap:{rule.id}:{r.key}"})
    existing = await _open_alert(db, rule.id, r.key)
    breach_since, clear_since = await _load_state(db, rule.id, r.key)
    d = decide(value=r.value, condition=condition, open_incident=existing is not None,
               breach_since=breach_since, clear_since=clear_since,
               hold_s=int(getattr(rule, "min_duration", 0) or 0),
               clear_hold_s=clear_hold_seconds(getattr(rule, "clear_duration", None)), now=now)
    if r.value is None:
        if existing:
            await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                                  "|| CAST(:m AS jsonb) WHERE id = :id"),
                             {"id": existing[0], "m": json.dumps({"data_state": "missing",
                                                                  "clearing_since": None})})
        if breach_since or clear_since:
            await db.execute(text(
                "UPDATE service_metric_alert_state SET breach_since = NULL, clear_since = NULL, "
                "updated_at = :now WHERE rule_id = :rid AND asset_key = :k"),
                {"rid": str(rule.id), "k": r.key, "now": now})
        await db.commit()
        return
    if d.breach_since or d.clear_since or breach_since or clear_since or existing:
        await _save_state(db, rule.id, r.key, d.breach_since, d.clear_since, r.value, now)
    if existing:
        await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                              "|| CAST(:m AS jsonb) WHERE id = :id"),
                         {"id": existing[0], "m": json.dumps({
                             "gap_minutes": round(float(r.value), 1), "data_state": "current",
                             "clearing_since": d.clear_since.isoformat() if d.clear_since else None})})
    if d.action == "raise":
        msg = sentence(metric, r) + "."
        alert_id = await _raise(db, rule, metric, r, condition, msg, now)
        await db.commit()
        if alert_id is not None:
            counts["raised"] += 1
            sent = await _notify(db, rule, metric, r, condition, event_sentence=msg,
                                 alert_id=alert_id, first_seen=now, now=now)
            await ns.stamp(db, alert_id, sent)
            await db.commit()
        return
    if d.action == "resolve" and existing:
        alert_id, started = existing
        msg = sentence(metric, r, recovery=True) + "."
        notified = await ns.was_notified(db, alert_id)
        counts["resolved"] += await _resolve(db, alert_id, now, {"recovery_message": msg})
        await db.commit()
        if notified and getattr(rule, "recovery_alert", False):
            await _notify(db, rule, metric, r, condition, event_sentence=msg, alert_id=alert_id,
                          first_seen=started, now=now, recovery=True)
        await db.commit()
        return
    if existing and breaching(r.value, condition) and rule.notify_channels and \
            await ns.is_pending(db, existing[0]) and await _trigger_allowed(db, rule, r.key, now) and \
            await ns.claim_pending(db, existing[0]):
        # Quiet hours held the trigger and the gap is still open: send it now.
        await db.commit()
        sent = await _notify(db, rule, metric, r, condition, event_sentence=sentence(metric, r) + ".",
                             alert_id=existing[0], first_seen=existing[1], now=now, checked=True)
        await ns.stamp(db, existing[0], sent)
    await db.commit()


# ─── Pass ────────────────────────────────────────────────────────────────────

_RULE_SQL = """
SELECT id, name, metric, operator, threshold, conditions, severity, min_duration, clear_duration,
       notify_channels, cooldown, device_id, server_id, group_id, device_type, location, scope_tag,
       profile_id, recovery_alert, email_subject, email_body, sms_template, recovery_email_subject,
       recovery_email_body, recovery_sms_template, schedule_start, schedule_end, schedule_days, {prio}
FROM alert_rules
WHERE enabled = true AND metric IN ('snmp_unreachable', 'agent_no_metrics')
"""


async def load_rules(db: AsyncSession) -> list:
    prio = await funnel.rule_priority_column(db)
    return (await db.execute(text(_RULE_SQL.format(prio=prio)))).all()


async def evaluate_monitoring_gap_rules(db: AsyncSession, *, now: Optional[datetime] = None) -> dict[str, int]:
    """One pass over every enabled snmp_unreachable / agent_no_metrics rule."""
    now = now or datetime.now(timezone.utc)
    counts = {"rules": 0, "raised": 0, "resolved": 0}
    rules = await load_rules(db)
    counts["rules"] = len(rules)
    if not rules:
        return counts
    metrics = {r.metric for r in rules}
    from app.services import alert_rule_exclusions
    excluded = await alert_rule_exclusions.load(db)
    devices: dict[str, dict] = {}
    servers: dict[str, dict] = {}
    family = None
    if SNMP_UNREACHABLE in metrics:
        try:
            async with db.begin_nested():
                devices = await load_snmp_devices(db)
        except Exception:
            logger.warning("SNMP poll state unavailable (migrate-202 not applied?); "
                           "snmp_unreachable rules wait", exc_info=True)
            devices = {}
        from app.services.device_templates import family as tfamily
        family = await tfamily.load(db)
    if AGENT_NO_METRICS in metrics:
        servers = await load_agent_servers(db)
    from app.api.v1.alert_engine import _device_in_maintenance
    maintenance: dict[str, bool] = {}

    for rule in rules:
        condition = rule_condition(rule)
        metric = condition.get("metric") or rule.metric
        if metric != rule.metric or metric not in METRICS:
            logger.error("Monitoring gap rule %s skipped: conditions must use one metric", rule.id)
            continue
        readings: list[Reading] = []
        if metric == SNMP_UNREACHABLE:
            for did, dev in devices.items():
                if dev.get("status") == "maintenance" or not device_rule_covers(rule, did, dev, family):
                    continue
                if excluded is not None and excluded.device(rule.id, did):
                    continue
                if did not in maintenance:
                    maintenance[did] = await _device_in_maintenance(db, did)
                if maintenance[did]:
                    continue
                readings.append(Reading(
                    key=f"gap:device:{did}", value=snmp_reading(dev, now), asset=device_asset(dev),
                    hostname=dev["hostname"], ip=dev["ip"], device_id=did,
                    context={"last_error": dev["last_error"][:200] or None,
                             "consecutive_failures": dev["consecutive_failures"]}))
        else:
            for sid, srv in servers.items():
                if srv.get("ping") == "maintenance" or srv.get("maintenance") \
                        or not server_rule_covers(rule, sid, srv):
                    continue
                if excluded is not None and excluded.server(rule.id, sid):
                    continue
                readings.append(Reading(
                    key=f"gap:server:{sid}", value=agent_reading(srv["heartbeat_at"], srv["last_metric_at"], now),
                    asset=server_asset(srv), hostname=srv["hostname"], ip=srv["ip"], server_id=sid))
        for r in readings:
            try:
                await apply_reading(db, rule, metric, r, condition, now, counts)
            except Exception:
                logger.exception("Monitoring gap rule %s failed for %s", rule.id, r.key)
                await db.rollback()
    if counts["raised"] or counts["resolved"]:
        logger.info("monitoring gap alert eval: %d rules, %d raised, %d resolved",
                    counts["rules"], counts["raised"], counts["resolved"])
    return counts


async def run_monitoring_gap_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import MONITORING_GAP_ALERT_PASS, run_pass
    return await run_pass(MONITORING_GAP_ALERT_PASS, evaluate_monitoring_gap_rules, session_factory)


async def monitoring_gap_alert_evaluator_loop() -> None:
    await asyncio.sleep(40)  # let the app boot and the first polls land
    while True:
        try:
            await run_monitoring_gap_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("monitoring gap alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
