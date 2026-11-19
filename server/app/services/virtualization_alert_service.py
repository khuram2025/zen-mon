"""Virtualisation alert rules (alerting plan G-11).

The virtualisation connectors (``virtualization_service``) store each poll's
inventory in ``virtualization_connectors.snapshot``: resources (clusters,
hosts, VMs, datastores) with their metrics and attributes, and the alarms the
platform reports. Three rule metrics read that snapshot, evaluated every
``EVAL_INTERVAL_S`` (one API worker at a time, ``alert_worker_lock``):

``vm_host_disconnected``
    1 while vCenter reports an ESXi host ``disconnected`` or ``notResponding``,
    0 while it reports it ``connected``. Dimension = the host. Only vSphere
    reports a host's connection state; a host vCenter has in maintenance mode
    is not evaluated.
``vm_datastore_pct``
    Used % of a datastore (Nutanix: storage container). Dimension = the
    datastore.
``vm_alarm``
    An active platform alarm (vCenter triggered alarms, Prism alerts):
    2 = red / critical, 1 = yellow / warning, 0 = cleared or another colour.
    Dimension = the alarm. ``vm_alarm >= 1`` covers red and yellow, ``>= 2``
    red only. An alarm that no longer appears in a current snapshot has
    cleared (value 0).

Asset = the connector (the vCenter / Prism / Hyper-V source). Its *linked
device* is the monitored device whose IP address or hostname is the
connector's endpoint host: the alert row carries it, a rule's ``device_id``
selects connectors by it, and its maintenance (manual or a window) pauses
evaluation of the whole source (open incidents stay as they are).
``target`` = one connector id, ``scope_tag`` = a connector tag.

Lifecycle (the metric-alert rules in AGENTS.md): one incident per rule + asset
+ dimension; the condition must hold for ``min_duration`` before it raises; an
open incident resolves only after its clear level held for ``clear_duration``
(NULL = 300 s, 0 = at once). The clear level is the condition's explicit
``reset_threshold``, else 2 points on the healthy side for the percent metric
and the trigger level itself for the state metrics. A missing reading (the
connector's snapshot is older than two poll intervals, the connector is
disabled or the resource left the inventory) never resolves and never counts
toward a hold. Every send goes through ``dispatch_to_channels`` with the alert
id and delivery event; a recovery is sent only when the trigger was and the
rule asks for one (``recovery_alert``).

The breach/clear start times live in ``virtualization_alert_state``
(migrate-201) so holds survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_funnel as funnel
from app.services import alert_notify_state as ns
from app.services import service_metric_alert_service as lifecycle

logger = logging.getLogger("zenplus.virtualization_alerts")

HOST_DISCONNECTED = "vm_host_disconnected"
DATASTORE_PCT = "vm_datastore_pct"
ALARM = "vm_alarm"
METRICS = frozenset({HOST_DISCONNECTED, DATASTORE_PCT, ALARM})
STATE_METRICS = frozenset({HOST_DISCONNECTED, ALARM})
# The values a state metric can take; a rule must fire on the worst and never on 0.
STATE_VALUES = {HOST_DISCONNECTED: (0.0, 1.0), ALARM: (0.0, 1.0, 2.0)}

EVAL_INTERVAL_S = 60
PERCENT_CLEAR_MARGIN = 2.0
EVENT_FAMILY = "virtualization"

_DISCONNECTED = {"disconnected": "disconnected", "notresponding": "not_responding"}
_ALARM_LEVEL = {"critical": 2.0, "red": 2.0, "warning": 1.0, "yellow": 1.0}
_HIGH = (">", ">=", "gt", "gte")
_LOW = ("<", "<=", "lt", "lte")


# ─── Conditions ──────────────────────────────────────────────────────────────

def clear_level(condition: dict) -> float:
    """The explicit reset_threshold, else 2 points on the healthy side of a
    percent trigger; the state metrics clear at the trigger level."""
    if condition.get("reset_threshold") is not None:
        return float(condition["reset_threshold"])
    thr, op = float(condition["threshold"]), condition.get("operator")
    if condition.get("metric") == DATASTORE_PCT:
        if op in _HIGH:
            return thr - PERCENT_CLEAR_MARGIN
        if op in _LOW:
            return thr + PERCENT_CLEAR_MARGIN
    return thr


def lifecycle_condition(condition: dict) -> dict:
    """The condition with its effective clear level, for the shared lifecycle step."""
    return {**condition, "reset_threshold": clear_level(condition)}


# ─── Sources and readings ────────────────────────────────────────────────────

_PROVIDER_NOUNS = {"vmware": "vCenter", "nutanix_pc": "Prism Central", "nutanix_pe": "Prism Element",
                   "hyperv": "Hyper-V"}
_HOST_NOUNS = {"vmware": "ESXi host", "hyperv": "Hyper-V host"}
_STORE_NOUNS = {"nutanix_pc": "Storage container", "nutanix_pe": "Storage container"}
_KIND_NOUNS = {"cluster": "Cluster", "vm": "VM"}


def source_label(connector: dict) -> str:
    """"vCenter vc-01"; just the name when it already says what it is."""
    name = str(connector.get("name") or "").strip() or endpoint_host(connector.get("endpoint")) or "source"
    noun = _PROVIDER_NOUNS.get(str(connector.get("provider") or ""), "Virtualisation source")
    return name if noun.lower() in name.lower() else f"{noun} {name}"


def resource_label(connector: dict, resource: Optional[dict]) -> str:
    if not resource:
        return ""
    provider = str(connector.get("provider") or "")
    kind = resource.get("kind")
    if kind == "host":
        noun = _HOST_NOUNS.get(provider, "Host")
    elif kind == "storage":
        noun = _STORE_NOUNS.get(provider, "Datastore")
    else:
        noun = _KIND_NOUNS.get(kind, "Resource")
    return f"{noun} {resource.get('name') or resource.get('external_id') or ''}".strip()


def endpoint_host(endpoint) -> str:
    try:
        return (urlsplit(str(endpoint or "")).hostname or "").lower()
    except ValueError:
        return ""


def _aware(ts) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def stale_after_s(connector: dict) -> int:
    """The connector API's "stale" rule: older than two poll intervals (+30 s)."""
    return 2 * int(connector.get("interval_seconds") or 300) + 30


def snapshot_current(connector: dict, now: datetime) -> bool:
    last = _aware(connector.get("last_success_at"))
    return bool(connector.get("enabled", True)) and last is not None and \
        (now - last).total_seconds() <= stale_after_s(connector)


def host_state(resource: dict) -> tuple[Optional[float], str]:
    """(1/0 or None, state word) for a host resource."""
    attrs = resource.get("attributes") or {}
    state = str(attrs.get("connection") or "").strip()
    key = state.replace("_", "").lower()
    if key == "connected":
        return 0.0, "connected"
    if key in _DISCONNECTED:
        return 1.0, _DISCONNECTED[key]
    return None, ""


def in_platform_maintenance(resource: dict) -> bool:
    return str((resource.get("attributes") or {}).get("maintenance") or "").lower() == "yes"


def datastore_pct(resource: dict) -> Optional[float]:
    value = (resource.get("metrics") or {}).get("storage_usage_percent")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def alarm_level(alarm: dict) -> float:
    if alarm.get("resolved"):
        return 0.0
    return _ALARM_LEVEL.get(str(alarm.get("severity") or "").strip().lower(), 0.0)


@dataclass
class Reading:
    key: str                         # "vc:<connector>:host:<id>" | ":storage:<id>" | ":alarm:<id>"
    value: Optional[float]
    source: str                      # "vCenter vc-01"
    resource: str = ""               # "ESXi host esx-04" / "Datastore DS-01"
    dimension: str = ""
    connector_id: str = ""
    device_id: Optional[str] = None
    hostname: str = ""
    ip: str = ""
    state: str = ""                  # host: connected / disconnected / not_responding
    alarm: str = ""                  # alarm title
    colour: str = ""                 # alarm: red / yellow / critical / warning
    context: dict = field(default_factory=dict)


def readings_for(metric: str, connector: dict, now: datetime, open_keys: dict) -> list[Reading]:
    """Readings of one metric on one connector, plus a missing (or, for an
    alarm, cleared) reading for every open incident whose dimension is gone."""
    cid = str(connector["id"])
    current = snapshot_current(connector, now)
    snap = connector.get("snapshot") or {}
    resources = [r for r in snap.get("resources") or [] if isinstance(r, dict)]
    by_id = {r.get("id"): r for r in resources}
    src = source_label(connector)
    linked = connector.get("linked") or {}
    base = dict(source=src, connector_id=cid, device_id=linked.get("id"),
                hostname=linked.get("hostname") or src, ip=linked.get("ip") or "")
    out: list[Reading] = []
    if metric in (HOST_DISCONNECTED, DATASTORE_PCT):
        kind = "host" if metric == HOST_DISCONNECTED else "storage"
        for r in resources:
            if r.get("kind") != kind or not r.get("id"):
                continue
            if metric == HOST_DISCONNECTED:
                if in_platform_maintenance(r):
                    continue
                value, state = host_state(r)
            else:
                value, state = datastore_pct(r), ""
            out.append(Reading(key=f"vc:{cid}:{r['id']}", value=value if current else None,
                               resource=resource_label(connector, r), dimension=str(r.get("name") or r["id"]),
                               state=state, context={"resource_id": r["id"]}, **base))
    else:
        provider = str(connector.get("provider") or "")
        for a in snap.get("alerts") or []:
            if not isinstance(a, dict) or not a.get("id"):
                continue
            sev = str(a.get("severity") or "").lower()
            colour = {"critical": "red", "warning": "yellow"}.get(sev, sev) if provider == "vmware" else sev
            res = by_id.get(a.get("resource_id"))
            out.append(Reading(key=f"vc:{cid}:alarm:{a['id']}", value=alarm_level(a) if current else None,
                               resource=resource_label(connector, res), dimension=str(a["id"]),
                               alarm=str(a.get("title") or "Alarm")[:200], colour=colour,
                               context={"resource_id": a.get("resource_id"),
                                        "acknowledged_on_platform": bool(a.get("acknowledged"))},
                               **base))
    seen = {r.key for r in out}
    prefix = f"vc:{cid}:" + {HOST_DISCONNECTED: "host:", DATASTORE_PCT: "storage:", ALARM: "alarm:"}[metric]
    for key, meta in open_keys.items():
        if key in seen or not key.startswith(prefix):
            continue
        gone_alarm = metric == ALARM and current
        out.append(Reading(key=key, value=0.0 if gone_alarm else None,
                           resource=meta.get("resource") or "", dimension=meta.get("dimension") or "",
                           alarm=meta.get("alarm") or "", colour=meta.get("colour") or "",
                           state="", context={"resource_id": meta.get("resource_id"), "gone": True}, **base))
    return out


# ─── Sentences ───────────────────────────────────────────────────────────────

def _num(v: float, decimals: int) -> str:
    return f"{v:.{decimals}f}" if decimals else f"{round(v):d}"


def pct_text(value: float, threshold: float) -> str:
    """"97%"; within 5 points of the limit (alert_phrasing.NEAR_THRESHOLD_POINTS)
    one decimal, so 84.6% is never shown as "85%" (two when one still reads
    as the limit)."""
    v, t = float(value), float(threshold)
    if v == round(v) or abs(v - t) >= 5:
        return _num(v, 0) + "%"
    decimals = 2 if v != t and f"{v:.1f}" == f"{t:.1f}" else 1
    return _num(v, decimals) + "%"


def limit_text(threshold: float) -> str:
    t = float(threshold)
    return (_num(t, 0) if t == round(t) else f"{t:g}") + "%"


def _alarm_words(r: Reading, value: Optional[float]) -> str:
    if r.colour in ("red", "yellow", "critical", "warning"):
        return r.colour
    return {2.0: "red", 1.0: "yellow"}.get(value or 0.0, "active")


def sentence(metric: str, r: Reading, condition: dict, *, recovery: bool = False) -> str:
    if metric == DATASTORE_PCT:
        verb = "is back to" if recovery else "is"
        return (f"{r.resource} on {r.source} {verb} {pct_text(r.value, condition['threshold'])} full "
                f"(limit {limit_text(condition['threshold'])})")
    if metric == HOST_DISCONNECTED:
        if recovery:
            return f"{r.resource} is connected to {r.source} again"
        if r.state == "not_responding":
            return f"{r.resource} is not responding to {r.source}"
        return f"{r.resource} is disconnected from {r.source}"
    on = f" on {r.resource}" if r.resource else ""
    if recovery:
        if (r.value or 0) > 0:
            return f'The alarm "{r.alarm}"{on} is now {_alarm_words(r, r.value)} in {r.source}'
        return f'The alarm "{r.alarm}"{on} cleared in {r.source}'
    return f'{r.source} raised the {_alarm_words(r, r.value)} alarm "{r.alarm}"{on}'


def reading_text(metric: str, r: Reading) -> str:
    if r.value is None:
        return ""
    if metric == DATASTORE_PCT:
        return f"{r.value:.1f}% used"
    if metric == HOST_DISCONNECTED:
        return {"connected": "Connected", "not_responding": "Not responding"}.get(r.state, "Disconnected")
    return _alarm_words(r, r.value).capitalize() if r.value else "Cleared"


METRIC_NOUNS = {HOST_DISCONNECTED: "Host connection", DATASTORE_PCT: "Datastore usage", ALARM: "Platform alarm"}


# ─── Scope ───────────────────────────────────────────────────────────────────

def rule_covers(rule, connector: dict) -> bool:
    """target (connector id), device_id (linked device) and scope_tag (connector tag), ANDed."""
    target = (getattr(rule, "target", None) or "").strip()
    if target and target != str(connector["id"]):
        return False
    if getattr(rule, "device_id", None) and \
            str(rule.device_id) != str((connector.get("linked") or {}).get("id") or ""):
        return False
    tag = (getattr(rule, "scope_tag", None) or "").strip().lower()
    if tag and tag not in {str(t).strip().lower() for t in connector.get("tags") or []}:
        return False
    return True


async def load_connectors(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT id::text, name, provider, endpoint, tags, enabled, interval_seconds, status, "
        "last_success_at, snapshot FROM virtualization_connectors"))).mappings().all()
    out = []
    for r in rows:
        c = dict(r)
        for k in ("tags", "snapshot"):
            if isinstance(c.get(k), str):
                try:
                    c[k] = json.loads(c[k])
                except ValueError:
                    c[k] = None
        out.append(c)
    hosts = {endpoint_host(c["endpoint"]) for c in out} - {""}
    linked: dict[str, dict] = {}
    if hosts:
        devs = (await db.execute(text(
            "SELECT id::text, hostname, host(ip_address) AS ip, status FROM devices "
            "WHERE host(ip_address) = ANY(CAST(:h AS text[])) OR lower(hostname) = ANY(CAST(:h AS text[])) "
            "ORDER BY created_at NULLS LAST, id"), {"h": sorted(hosts)})).mappings().all()
        # An address match wins over a name match; the oldest device wins a tie.
        for field in ("ip", "hostname"):
            for d in devs:
                key = (d[field] or "").lower()
                if key in hosts and key not in linked:
                    linked[key] = dict(d)
    for c in out:
        c["linked"] = linked.get(endpoint_host(c["endpoint"]))
    return out


# ─── Alert rows ──────────────────────────────────────────────────────────────

async def _open_alerts(db: AsyncSession, rule_id) -> dict[str, tuple]:
    rows = (await db.execute(text(
        "SELECT id, triggered_at, metadata FROM alerts WHERE rule_id = :rid "
        "AND metadata->>'virt_alert_key' IS NOT NULL AND status IN ('active','acknowledged') "
        "ORDER BY triggered_at"), {"rid": str(rule_id)})).all()
    out = {}
    for aid, at, meta in rows:
        meta = meta if isinstance(meta, dict) else (json.loads(meta) if meta else {})
        out[meta.get("virt_alert_key")] = (aid, at, meta)
    return out


async def _open_alert(db: AsyncSession, rule_id, key: str):
    row = (await db.execute(text(
        "SELECT id, triggered_at, metadata FROM alerts WHERE rule_id = :rid "
        "AND metadata->>'virt_alert_key' = :k AND status IN ('active','acknowledged') "
        "ORDER BY triggered_at DESC LIMIT 1"), {"rid": str(rule_id), "k": key})).first()
    if not row:
        return None
    meta = row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else {})
    return row[0], row[1], meta


async def _load_state(db: AsyncSession, rule_id, key: str):
    row = (await db.execute(text(
        "SELECT breach_since, clear_since FROM virtualization_alert_state "
        "WHERE rule_id = :rid AND asset_key = :k"), {"rid": str(rule_id), "k": key})).first()
    return (_aware(row[0]), _aware(row[1])) if row else (None, None)


async def _save_state(db: AsyncSession, rule_id, key: str, breach_since, clear_since,
                      value, now: datetime) -> None:
    await db.execute(text(
        "INSERT INTO virtualization_alert_state "
        "(rule_id, asset_key, breach_since, clear_since, last_value, last_seen_at, updated_at) "
        "VALUES (:rid, :k, :b, :c, :v, :now, :now) "
        "ON CONFLICT (rule_id, asset_key) DO UPDATE SET breach_since = EXCLUDED.breach_since, "
        "clear_since = EXCLUDED.clear_since, last_value = EXCLUDED.last_value, "
        "last_seen_at = EXCLUDED.last_seen_at, updated_at = EXCLUDED.updated_at"),
        {"rid": str(rule_id), "k": key, "b": breach_since, "c": clear_since, "v": value, "now": now})


def _metadata(rule, metric: str, r: Reading, condition: dict) -> dict:
    meta = {"rule_id": str(rule.id), "metric": metric, "virt_alert_key": r.key,
            "connector_id": r.connector_id, "source": r.source, "resource": r.resource or None,
            "dimension": r.dimension or None,
            "value": None if r.value is None else round(float(r.value), 2),
            "threshold": condition["threshold"], "clear_level": clear_level(condition),
            "data_state": "current"}
    if metric == ALARM:
        meta.update(alarm=r.alarm, colour=r.colour or None)
    if metric == HOST_DISCONNECTED:
        meta["connection"] = r.state or None
    meta.update({k: v for k, v in r.context.items() if k != "gone"})
    return meta


async def _raise(db: AsyncSession, rule, metric: str, r: Reading, condition: dict,
                 message: str, now: datetime):
    meta = {**_metadata(rule, metric, r, condition), **ns.sending_metadata()}
    p_col, p_val, p_params = funnel.priority_insert(
        await funnel.priority_supported(db), getattr(rule, "priority", None), rule.severity or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, "
        f"metadata{p_col}) VALUES (:did, :rid, 'active', :sev, :msg, :ts, CAST(:meta AS jsonb){p_val}) "
        f"RETURNING id"),
        {"did": r.device_id, "rid": str(rule.id), "sev": rule.severity or "warning", "msg": message,
         "ts": now, "meta": json.dumps(meta, default=str), **p_params})).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id, now: datetime, extra: dict) -> int:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"),
        {"ts": now, "id": alert_id, "m": json.dumps({
            "resolved_by": "virtualization_evaluator", "resolved_at": now.isoformat(), **extra},
            default=str)})
    return res.rowcount or 0


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
    reading = reading_text(metric, r)
    limit = limit_text(condition["threshold"]) if metric == DATASTORE_PCT else ""
    asset = f"{r.resource} on {r.source}" if r.resource else r.source
    variables = {
        "severity": sev.upper(), "status": "RESOLVED" if recovery else "ALERT",
        "rule_name": rule.name or "Alert", "host_suffix": f" — {asset}",
        "hostname": r.hostname or r.source, "ip_address": r.ip, "asset": asset,
        "source": r.source, "resource": r.resource, "alarm": r.alarm, "reading": reading,
        "threshold": limit, "metric": METRIC_NOUNS.get(metric, metric),
        "operator": condition.get("operator") or "", "event_sentence": event_sentence,
        "duration": duration, "duration_sentence": dur_sentence, "duration_suffix": dur_sentence,
        "timestamp": now.strftime("%H:%M UTC on %d %b %Y"), **incident,
    }
    # {site}, {group}, {site_suffix} of the asset (notification_context).
    from app.services import notification_context
    variables.update(await notification_context.asset_place(db, {"alert_id": alert_id}))
    prefix = "recovery_" if recovery else ""
    defaults = {
        "email_subject": ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if recovery else ap.DEFAULT_EMAIL_SUBJECT,
        "email_body": ap.DEFAULT_RECOVERY_EMAIL_BODY if recovery else ap.DEFAULT_EMAIL_BODY,
        "sms_template": ap.DEFAULT_RECOVERY_SMS if recovery else ap.DEFAULT_SMS,
    }
    subject = _render(ap.effective_template(getattr(rule, prefix + "email_subject", None),
                                            defaults["email_subject"]), variables)
    body = _render(ap.effective_template(getattr(rule, prefix + "email_body", None),
                                         defaults["email_body"]), variables)
    sms = _render(ap.effective_template(getattr(rule, "recovery_sms_template" if recovery else "sms_template",
                                                None), defaults["sms_template"]), variables)
    details = [("Alert rule", rule.name), ("Source", r.source), ("Resource", r.resource or None)]
    if metric == ALARM:
        details.append(("Alarm", r.alarm or None))
    details += [("Limit", limit or None), ("Active for", duration or None)]
    sent = await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": subject, "body": body, "message": sms,
        "hostname": r.hostname or r.source, "ip_address": r.ip,
        "status": "RESOLVED" if recovery else "ALERT", "severity": sev, "resolved": recovery,
        "rule_name": rule.name, "metric": metric, "event_family": EVENT_FAMILY,
        "headline_metric": {"label": METRIC_NOUNS.get(metric, metric), "value": reading,
                            **({"secondary_label": "Limit", "secondary_value": limit} if limit else {})},
        "details": details,
        "triggered_at": now.isoformat(), "rule_id": str(rule.id),
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": "recovery" if recovery else "trigger", "is_recovery": recovery,
        **funnel.notification_fields(sev, getattr(rule, "priority", None), trigger=not recovery),
        **incident,
    })
    return bool(sent)


# ─── One rule + asset + dimension ────────────────────────────────────────────

async def apply_reading(db: AsyncSession, rule, metric: str, r: Reading, condition: dict,
                        now: datetime, counts: dict) -> None:
    """Run the lifecycle for one reading, committing as it goes."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"virt_alert:{rule.id}:{r.key}"})
    existing = await _open_alert(db, rule.id, r.key)
    breach_since, clear_since = await _load_state(db, rule.id, r.key)
    d = lifecycle.decide(value=r.value, condition=lifecycle_condition(condition),
                         open_incident=existing is not None, breach_since=breach_since,
                         clear_since=clear_since, hold_s=int(getattr(rule, "min_duration", 0) or 0),
                         clear_hold_s=lifecycle.clear_hold_seconds(getattr(rule, "clear_duration", None)),
                         now=now)
    if r.value is None:
        if existing:
            await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                                  "|| CAST(:m AS jsonb) WHERE id = :id"),
                             {"id": existing[0], "m": json.dumps({"data_state": "missing",
                                                                  "clearing_since": None})})
        if breach_since or clear_since:
            await db.execute(text(
                "UPDATE virtualization_alert_state SET breach_since = NULL, clear_since = NULL, "
                "updated_at = :now WHERE rule_id = :rid AND asset_key = :k"),
                {"rid": str(rule.id), "k": r.key, "now": now})
        await db.commit()
        return
    await _save_state(db, rule.id, r.key, d.breach_since, d.clear_since, r.value, now)
    if existing:
        await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                              "|| CAST(:m AS jsonb) WHERE id = :id"),
                         {"id": existing[0], "m": json.dumps({
                             "value": round(float(r.value), 2), "data_state": "current",
                             "clearing_since": d.clear_since.isoformat() if d.clear_since else None})})
    if d.action == "raise":
        msg = sentence(metric, r, condition) + "."
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
        alert_id, started, meta = existing
        r.resource = r.resource or meta.get("resource") or ""
        r.alarm = r.alarm or meta.get("alarm") or ""
        msg = sentence(metric, r, condition, recovery=True) + "."
        notified = await ns.was_notified(db, alert_id)
        counts["resolved"] += await _resolve(db, alert_id, now, {
            "recovery_value": round(float(r.value), 2), "recovery_message": msg})
        await db.commit()
        if notified and getattr(rule, "recovery_alert", False):
            await _notify(db, rule, metric, r, condition, event_sentence=msg, alert_id=alert_id,
                          first_seen=started, now=now, recovery=True)
        await db.commit()
        return
    if existing and lifecycle.cmp(r.value, condition.get("operator"), condition["threshold"]) and \
            rule.notify_channels and await ns.is_pending(db, existing[0]) and \
            await _trigger_allowed(db, rule, r.key, now) and await ns.claim_pending(db, existing[0]):
        # Quiet hours held the trigger and the condition still breaches: send it now.
        await db.commit()
        sent = await _notify(db, rule, metric, r, condition, event_sentence=sentence(metric, r, condition) + ".",
                             alert_id=existing[0], first_seen=existing[1], now=now, checked=True)
        await ns.stamp(db, existing[0], sent)
    await db.commit()


async def _trigger_allowed(db: AsyncSession, rule, key: str, now: datetime) -> bool:
    """Outside the rule's cooldown for this asset and inside its schedule window."""
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    cooldown = int(getattr(rule, "cooldown", 0) or 0)
    if cooldown > 0:
        prior = (await db.execute(text(
            "SELECT 1 FROM alerts WHERE rule_id = :rid AND metadata->>'virt_alert_key' = :k "
            "AND metadata->>'notified' = 'true' AND triggered_at > :since LIMIT 1"),
            {"rid": str(rule.id), "k": key, "since": now - timedelta(seconds=cooldown)})).first()
        if prior:
            return False
    tz = await get_configured_timezone(db)
    return notifications_allowed(getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
                                 getattr(rule, "schedule_days", None), tz)


# ─── Pass ────────────────────────────────────────────────────────────────────

_RULE_SQL = """
SELECT id, name, metric, operator, threshold, conditions, severity, min_duration, clear_duration,
       notify_channels, cooldown, device_id, scope_tag, target, recovery_alert,
       email_subject, email_body, sms_template, recovery_email_subject, recovery_email_body,
       recovery_sms_template, schedule_start, schedule_end, schedule_days, {prio}
FROM alert_rules
WHERE enabled = true AND metric IN ('vm_host_disconnected', 'vm_datastore_pct', 'vm_alarm')
"""


async def load_rules(db: AsyncSession) -> list:
    prio = await funnel.rule_priority_column(db)
    return (await db.execute(text(_RULE_SQL.format(prio=prio)))).all()


async def evaluate_virtualization_rules(db: AsyncSession, *, now: Optional[datetime] = None) -> dict[str, int]:
    """One pass over every enabled virtualisation rule."""
    now = now or datetime.now(timezone.utc)
    counts = {"rules": 0, "raised": 0, "resolved": 0}
    rules = await load_rules(db)
    counts["rules"] = len(rules)
    if not rules:
        return counts
    connectors = await load_connectors(db)
    from app.api.v1.alert_engine import _device_in_maintenance
    paused: dict[str, bool] = {}
    for c in connectors:
        dev = c.get("linked")
        if dev:
            paused[c["id"]] = dev.get("status") == "maintenance" or \
                await _device_in_maintenance(db, dev["id"])
    for rule in rules:
        condition = lifecycle.rule_condition(rule)
        metric = condition.get("metric") or rule.metric
        if metric != rule.metric or metric not in METRICS:
            logger.error("Virtualisation rule %s skipped: conditions must use one metric", rule.id)
            continue
        open_keys = {k: v[2] for k, v in (await _open_alerts(db, rule.id)).items() if k}
        readings: list[Reading] = []
        for c in connectors:
            if not rule_covers(rule, c) or paused.get(c["id"]):
                continue
            readings.extend(readings_for(metric, c, now, open_keys))
        for r in readings:
            try:
                await apply_reading(db, rule, metric, r, condition, now, counts)
            except Exception:
                logger.exception("Virtualisation rule %s failed for %s", rule.id, r.key)
                await db.rollback()
    if counts["raised"] or counts["resolved"]:
        logger.info("virtualisation alert eval: %d rules, %d raised, %d resolved",
                    counts["rules"], counts["raised"], counts["resolved"])
    return counts


async def run_virtualization_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import VIRTUALIZATION_ALERT_PASS, run_pass
    return await run_pass(VIRTUALIZATION_ALERT_PASS, evaluate_virtualization_rules, session_factory)


async def virtualization_alert_evaluator_loop() -> None:
    await asyncio.sleep(40)  # let the app boot and the connectors poll
    while True:
        try:
            await run_virtualization_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("virtualisation alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
