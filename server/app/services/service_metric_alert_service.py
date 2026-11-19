"""Certificate-expiry and response-time alert rules (alerting plan G-4).

Three rule metrics that are levels, not status transitions, evaluated every
``EVAL_INTERVAL_S`` (one API worker at a time, ``alert_worker_lock``):

``service_tls_days``
    Days until the certificate a service check saw expires (from
    ``service_checks.tls_expiry_date``, else ``tls_days_remaining``). Only a
    check whose latest run is fresh (within 3 check intervals, at least 15
    minutes) and returned a certificate has a reading.
``service_response_ms``
    Average response time of the check's *passing* runs (ClickHouse
    ``service_metrics``, ``is_up = 1``) over ``max(min_duration, 300 s)``, from
    every vantage; when no run fell inside that window but the latest passing
    run is within two check intervals, that run. Failed runs never count: a
    failing check is the status rules' business.
``f5_cert_days``
    Days until each certificate in an F5 BIG-IP's certificate inventory
    (``device_template_values`` ``f5_cert_expiry_epoch``) expires. Asset = the
    device, dimension = the certificate. Readings older than ``F5_STALE_S``
    are missing. Inventory does not prove a certificate is bound to traffic.

Lifecycle (the metric-alert rules in AGENTS.md): one incident per rule + asset
+ dimension; the condition must hold for ``min_duration`` before it raises; an
open incident resolves only after its clear level held for ``clear_duration``
(NULL = 300 s, 0 = at once). The clear level is the condition's explicit
``reset_threshold``, else one day on the healthy side for the day metrics and
10 % for response time. A missing reading never resolves (and never counts
toward a hold). Every send goes through ``dispatch_to_channels`` with the
alert id and delivery event; a recovery is sent only when the trigger was and
the rule asks for one (``recovery_alert``).

Service checks inside an active service maintenance window and devices in
maintenance are skipped (an open incident stays as it is). Shared rules
excluded for a device (``alert_rule_exclusions``) skip that F5.

Each rule takes a single condition. The breach/clear start times live in
``service_metric_alert_state`` (migrate-199) so holds survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_funnel as funnel
from app.services import alert_notify_state as ns
from app.services import service_metric_phrasing as sp

logger = logging.getLogger("zenplus.service_metric_alerts")

TLS_DAYS = "service_tls_days"
RESPONSE_MS = "service_response_ms"
F5_CERT_DAYS = "f5_cert_days"
SERVICE_METRICS = frozenset({TLS_DAYS, RESPONSE_MS})
DEVICE_METRICS = frozenset({F5_CERT_DAYS})
METRICS = SERVICE_METRICS | DEVICE_METRICS
DAYS_METRICS = frozenset({TLS_DAYS, F5_CERT_DAYS})

EVAL_INTERVAL_S = 60
DEFAULT_CLEAR_S = 300
RESPONSE_MIN_WINDOW_S = 300
DAYS_CLEAR_MARGIN = 1.0
RESPONSE_CLEAR_FRACTION = 0.10
SERVICE_STALE_FLOOR_S = 900
F5_STALE_S = 3 * 3600 + 900       # the inventory group polls hourly; three polls missed
F5_EPOCH_KEY = "f5_cert_expiry_epoch"

_HIGH = (">", ">=", "gt", "gte")
_LOW = ("<", "<=", "lt", "lte")
_OPS = {
    ">": lambda a, b: a > b, "gt": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "gte": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "lt": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, "lte": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "eq": lambda a, b: a == b,
    "!=": lambda a, b: a != b, "neq": lambda a, b: a != b,
}


# ─── Conditions ──────────────────────────────────────────────────────────────

def cmp(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get((operator or "").strip())
    return bool(fn and fn(float(value), float(threshold)))


def rule_condition(rule) -> dict:
    """The rule's single condition: {metric, operator, threshold, reset_threshold}."""
    conds = getattr(rule, "conditions", None)
    if isinstance(conds, str):
        try:
            conds = json.loads(conds)
        except ValueError:
            conds = None
    if conds:
        c = dict(conds[0])
    else:
        c = {"metric": rule.metric, "operator": rule.operator, "threshold": rule.threshold}
    c.setdefault("reset_threshold", None)
    c["threshold"] = float(c.get("threshold") or 0)
    return c


def clear_level(condition: dict) -> float:
    """The level an open incident must get past to clear: the explicit
    reset_threshold, else 1 day (days metrics) or 10 % (response time) on the
    healthy side of the trigger. Equality operators clear at the trigger."""
    if condition.get("reset_threshold") is not None:
        return float(condition["reset_threshold"])
    thr, op, metric = float(condition["threshold"]), condition.get("operator"), condition.get("metric")
    if metric in DAYS_METRICS:
        margin = DAYS_CLEAR_MARGIN
    elif metric == RESPONSE_MS:
        margin = abs(thr) * RESPONSE_CLEAR_FRACTION
    else:
        margin = 0.0
    if op in _HIGH:
        return thr - margin
    if op in _LOW:
        return thr + margin
    return thr


def clear_hold_seconds(clear_duration) -> int:
    return DEFAULT_CLEAR_S if clear_duration is None else max(0, int(clear_duration))


def response_window_s(min_duration) -> int:
    return max(int(min_duration or 0), RESPONSE_MIN_WINDOW_S)


@dataclass
class Decision:
    action: str                  # "raise" | "resolve" | "hold" | "none"
    breach_since: Optional[datetime]
    clear_since: Optional[datetime]


def decide(*, value: Optional[float], condition: dict, open_incident: bool,
           breach_since: Optional[datetime], clear_since: Optional[datetime],
           hold_s: int, clear_hold_s: int, now: datetime) -> Decision:
    """One step of the lifecycle for one rule + asset + dimension.

    A missing value never raises or resolves, and never counts toward a hold:
    a breach or clear that was being held starts over once data returns."""
    if value is None:
        return Decision("none", None, None)
    op, thr = condition.get("operator"), float(condition["threshold"])
    if not open_incident:
        if cmp(value, op, thr):
            since = breach_since or now
            if (now - since).total_seconds() >= hold_s:
                return Decision("raise", None, None)
            return Decision("hold", since, None)
        return Decision("none", None, None)
    still = cmp(value, op, clear_level(condition))
    if still:
        return Decision("none", None, None)
    since = clear_since or now
    if (now - since).total_seconds() >= clear_hold_s:
        return Decision("resolve", None, None)
    return Decision("hold", None, since)


# ─── Readings ────────────────────────────────────────────────────────────────

@dataclass
class Reading:
    key: str                         # "check:<id>" | "device:<id>:<instance>"
    value: Optional[float]
    asset: str                       # "Service check Shop HTTPS" / "Load balancer f5-01 (10.0.0.9)"
    hostname: str = ""
    ip: str = ""
    device_id: Optional[str] = None
    service_check_id: Optional[str] = None
    dimension: str = ""
    cert: str = ""
    expires_on: Optional[datetime] = None
    window_s: Optional[int] = None   # response time: averaging window (None = latest run)
    context: dict = field(default_factory=dict)


def _aware(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def check_stale_s(check: dict) -> int:
    return max(3 * int(check.get("check_interval") or 60), SERVICE_STALE_FLOOR_S)


def tls_reading(check: dict, now: datetime) -> tuple[Optional[float], Optional[datetime]]:
    """(days left, expiry) of the certificate a check's latest run saw, or (None, None)."""
    last = _aware(check.get("last_check_at"))
    if last is None or (now - last).total_seconds() > check_stale_s(check):
        return None, None
    expiry = _aware(check.get("tls_expiry_date"))
    if expiry is not None:
        return float(sp.days_until(expiry, now)), expiry
    days = check.get("tls_days_remaining")
    if days is None:
        return None, None
    return float(days), None


def fetch_response_stats(check_intervals: dict[str, int], window_s: int, now: datetime,
                         client=None) -> dict[str, dict]:
    """{check_id: {avg, count, last, last_at}} of passing runs (blocking; ClickHouse).

    ``avg``/``count`` cover the last ``window_s``; ``last``/``last_at`` the
    newest passing run within the lookback (two check intervals, at least the
    window), for checks that run less often than the window."""
    if not check_intervals:
        return {}
    if client is None:
        from app.core.database import get_clickhouse_client
        client = get_clickhouse_client()
    lookback = max([window_s] + [2 * int(i or 60) for i in check_intervals.values()])
    fmt = "%Y-%m-%d %H:%M:%S"
    rows = client.query(
        """
        SELECT toString(service_check_id) AS id,
               avgIf(response_ms, timestamp >= %(w_since)s) AS avg_w,
               countIf(timestamp >= %(w_since)s) AS n_w,
               argMax(response_ms, timestamp) AS last_v,
               max(timestamp) AS last_ts
        FROM zenplus.service_metrics
        WHERE service_check_id IN %(ids)s AND is_up = 1
          AND timestamp >= %(since)s AND timestamp <= %(until)s
        GROUP BY service_check_id
        """,
        parameters={"ids": sorted(check_intervals), "w_since": (now - timedelta(seconds=window_s)).strftime(fmt),
                    "since": (now - timedelta(seconds=lookback)).strftime(fmt),
                    "until": now.astimezone(timezone.utc).strftime(fmt)},
    ).result_rows
    out = {}
    for cid, avg_w, n_w, last_v, last_ts in rows:
        out[str(cid)] = {"avg": _finite(avg_w), "count": int(n_w or 0),
                         "last": _finite(last_v), "last_at": _aware(last_ts)}
    return out


def _finite(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def response_value(stats: Optional[dict], check: dict, now: datetime,
                   window_s: int) -> tuple[Optional[float], Optional[int]]:
    """(milliseconds, window seconds or None for the latest run), or (None, None)."""
    if not stats:
        return None, None
    if stats.get("count") and stats.get("avg") is not None:
        return stats["avg"], window_s
    last_at = stats.get("last_at")
    if stats.get("last") is not None and last_at is not None and \
            (now - last_at).total_seconds() <= 2 * int(check.get("check_interval") or 60):
        return stats["last"], None
    return None, None


# ─── Assets ──────────────────────────────────────────────────────────────────

async def load_service_checks(db: AsyncSession) -> dict[str, dict]:
    rows = (await db.execute(text(
        "SELECT sc.id::text, sc.name, sc.check_type, sc.device_id::text, sc.group_id::text, sc.tags, "
        "sc.target_host, sc.target_url, sc.check_interval, sc.last_check_at, sc.tls_expiry_date, "
        "sc.tls_days_remaining, sc.tls_subject, sc.status, d.hostname, host(d.ip_address) "
        "FROM service_checks sc LEFT JOIN devices d ON d.id = sc.device_id "
        "WHERE sc.enabled = true"))).all()
    return {r[0]: {"id": r[0], "name": r[1] or "check", "check_type": r[2] or "", "device_id": r[3],
                   "group_id": r[4], "tags": [str(t).lower() for t in (r[5] or [])],
                   "target_host": r[6] or "", "target_url": r[7] or "", "check_interval": r[8] or 60,
                   "last_check_at": r[9], "tls_expiry_date": r[10], "tls_days_remaining": r[11],
                   "tls_subject": r[12] or "", "status": r[13], "device_hostname": r[14] or "",
                   "device_ip": r[15] or ""}
            for r in rows}


async def load_f5_certificates(db: AsyncSession) -> tuple[dict[str, dict], list[dict]]:
    """({device_id: device}, [certificate rows]) for every device reporting an F5 inventory."""
    rows = (await db.execute(text(
        "SELECT v.device_id::text, v.instance, v.label, v.value_num, v.updated_at, "
        "d.hostname, host(d.ip_address), d.device_type, d.location, d.group_id::text, d.tags, "
        "d.profile_id::text, d.status "
        "FROM device_template_values v JOIN devices d ON d.id = v.device_id "
        "WHERE v.metric_key = :k"), {"k": F5_EPOCH_KEY})).all()
    from app.services.device_groups import membership
    from app.services.tag_service import tag_set
    groups = await membership.load(db, "device") if rows else None
    devices: dict[str, dict] = {}
    certs: list[dict] = []
    for r in rows:
        did = r[0]
        devices.setdefault(did, {
            "hostname": r[5] or "device", "ip": r[6] or "", "device_type": r[7], "location": r[8],
            "group_id": r[9], "tags": tag_set(r[10]), "profile_id": r[11], "status": r[12],
            "groups": (groups.get(did, set()) if groups is not None else None)})
        certs.append({"device_id": did, "instance": r[1] or "", "label": r[2] or "",
                      "epoch": _finite(r[3]), "updated_at": _aware(r[4])})
    return devices, certs


def f5_reading(cert: dict, now: datetime) -> tuple[Optional[float], Optional[datetime]]:
    at = cert.get("updated_at")
    epoch = cert.get("epoch")
    if at is None or (now - at).total_seconds() > F5_STALE_S or epoch is None or epoch <= 0:
        return None, None
    expiry = datetime.fromtimestamp(epoch, timezone.utc)
    return float(sp.days_until(expiry, now)), expiry


def service_rule_covers(rule, check: dict) -> bool:
    """Service check / service check group / device / tag scope (ANDed)."""
    if getattr(rule, "service_check_id", None) and str(rule.service_check_id) != check["id"]:
        return False
    if getattr(rule, "service_check_group_id", None) and \
            str(rule.service_check_group_id) != (check.get("group_id") or ""):
        return False
    if getattr(rule, "device_id", None) and str(rule.device_id) != (check.get("device_id") or ""):
        return False
    tag = (getattr(rule, "scope_tag", None) or "").strip().lower()
    if tag and tag not in check.get("tags", []):
        return False
    return True


def device_rule_covers(rule, did: str, dev: dict, family=None) -> bool:
    from app.services.network_alert_service import _device_in_scope
    return _device_in_scope(rule, did, dev, family)


def check_asset(check: dict) -> str:
    return sp.service_asset(check["name"])


def check_cert_name(check: dict) -> str:
    return sp.certificate_name(check.get("tls_subject"), check.get("target_host") or "")


def f5_asset(dev: dict) -> str:
    from app.services import alert_phrasing as ap
    return ap.asset_label(dev["hostname"], dev.get("ip"),
                          ap.asset_type_label(dev.get("device_type"), "Device"))


# ─── Sentences ───────────────────────────────────────────────────────────────

def sentence(metric: str, r: Reading, condition: dict, *, recovery: bool = False,
             renewed: bool = False) -> str:
    if metric == RESPONSE_MS:
        return sp.response_sentence(asset=r.asset, value=r.value, threshold=condition["threshold"],
                                    window_s=r.window_s, recovery=recovery) if r.window_s else \
            _latest_run_sentence(r, condition, recovery)
    return sp.certificate_sentence(cert=r.cert, asset=r.asset, days=r.value, expires_on=r.expires_on,
                                   threshold=condition["threshold"], recovery=recovery,
                                   renewed=renewed, joiner="on" if metric == F5_CERT_DAYS else "for")


def _latest_run_sentence(r: Reading, condition: dict, recovery: bool) -> str:
    verb = "is back to" if recovery else "is"
    return (f"Response time of {r.asset} {verb} {sp.ms_text(r.value)} "
            f"(limit {sp.ms_text(condition['threshold'])}, latest run)")


# ─── Alert rows ──────────────────────────────────────────────────────────────

async def _open_alert(db: AsyncSession, rule_id, key: str):
    row = (await db.execute(text(
        "SELECT id, triggered_at, metadata FROM alerts WHERE rule_id = :rid "
        "AND metadata->>'service_metric_key' = :k AND status IN ('active','acknowledged') "
        "ORDER BY triggered_at DESC LIMIT 1"), {"rid": str(rule_id), "k": key})).first()
    if not row:
        return None
    meta = row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else {})
    return row[0], row[1], meta


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
    meta = {"rule_id": str(rule.id), "metric": metric, "service_metric_key": r.key,
            "value": None if r.value is None else round(float(r.value), 2),
            "threshold": condition["threshold"], "clear_level": clear_level(condition),
            "dimension": r.dimension or None, "data_state": "current"}
    if metric in DAYS_METRICS:
        meta.update(certificate=r.cert or None,
                    expires_on=r.expires_on.isoformat() if r.expires_on else None)
    else:
        meta["window_s"] = r.window_s
    meta.update(r.context)
    return meta


async def _raise(db: AsyncSession, rule, metric: str, r: Reading, condition: dict,
                 message: str, now: datetime):
    meta = {**_metadata(rule, metric, r, condition), **ns.sending_metadata()}
    p_col, p_val, p_params = funnel.priority_insert(
        await funnel.priority_supported(db), getattr(rule, "priority", None), rule.severity or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, service_check_id, rule_id, status, severity, message, "
        f"triggered_at, metadata{p_col}) VALUES (:did, :scid, :rid, 'active', :sev, :msg, :ts, "
        f"CAST(:meta AS jsonb){p_val}) RETURNING id"),
        {"did": r.device_id, "scid": r.service_check_id, "rid": str(rule.id),
         "sev": rule.severity or "warning", "msg": message, "ts": now,
         "meta": json.dumps(meta, default=str), **p_params})).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id, now: datetime, extra: dict) -> int:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"),
        {"ts": now, "id": alert_id, "m": json.dumps({
            "resolved_by": "service_metric_evaluator", "resolved_at": now.isoformat(), **extra},
            default=str)})
    return res.rowcount or 0


async def _trigger_allowed(db: AsyncSession, rule, key: str, now: datetime) -> bool:
    """Outside the rule's cooldown for this asset and inside its schedule window."""
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    cooldown = int(getattr(rule, "cooldown", 0) or 0)
    if cooldown > 0:
        prior = (await db.execute(text(
            "SELECT 1 FROM alerts WHERE rule_id = :rid AND metadata->>'service_metric_key' = :k "
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
    reading = sp.value_text(metric, r.value)
    limit = sp.limit_text(metric, condition["threshold"])
    variables = {
        "severity": sev.upper(), "status": "RESOLVED" if recovery else "ALERT",
        "rule_name": rule.name or "Alert", "host_suffix": f" — {r.asset}",
        "hostname": r.hostname or r.asset, "ip_address": r.ip, "asset": r.asset,
        "check_name": r.context.get("check_name", ""), "check_type": r.context.get("check_type", ""),
        "target": r.context.get("target", ""), "certificate": r.cert,
        "expires_on": sp.date_text(r.expires_on), "reading": reading, "threshold": limit,
        "metric": sp.METRIC_NOUNS.get(metric, metric), "operator": condition.get("operator") or "",
        "event_sentence": event_sentence, "duration": duration,
        "duration_sentence": dur_sentence, "duration_suffix": dur_sentence,
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
    details = [("Alert rule", rule.name), ("Asset", r.asset)]
    if metric in DAYS_METRICS:
        details += [("Certificate", r.cert or None), ("Expires on", sp.date_text(r.expires_on) or None)]
    details += [("Limit", limit or None), ("Active for", duration or None)]
    sent = await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": subject, "body": body, "message": sms,
        "hostname": r.hostname or r.asset, "ip_address": r.ip,
        "status": "RESOLVED" if recovery else "ALERT", "severity": sev, "resolved": recovery,
        "rule_name": rule.name, "metric": metric,
        "event_family": "certificate" if metric in DAYS_METRICS else "response_time",
        "headline_metric": {"label": sp.METRIC_NOUNS.get(metric, metric), "value": reading,
                            "secondary_label": "Limit", "secondary_value": limit},
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
                     {"k": f"service_metric:{rule.id}:{r.key}"})
    existing = await _open_alert(db, rule.id, r.key)
    breach_since, clear_since = await _load_state(db, rule.id, r.key)
    hold_s = int(getattr(rule, "min_duration", 0) or 0)
    d = decide(value=r.value, condition=condition, open_incident=existing is not None,
               breach_since=breach_since, clear_since=clear_since, hold_s=hold_s,
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
        renewed = (metric in DAYS_METRICS and r.expires_on is not None
                   and meta.get("expires_on") not in (None, r.expires_on.isoformat()))
        msg = sentence(metric, r, condition, recovery=True, renewed=renewed) + "."
        notified = await ns.was_notified(db, alert_id)
        counts["resolved"] += await _resolve(db, alert_id, now, {
            "recovery_value": round(float(r.value), 2), "recovery_message": msg, "renewed": renewed})
        await db.commit()
        if notified and getattr(rule, "recovery_alert", False):
            await _notify(db, rule, metric, r, condition, event_sentence=msg, alert_id=alert_id,
                          first_seen=started, now=now, recovery=True)
        await db.commit()
        return
    if existing and cmp(r.value, condition.get("operator"), condition["threshold"]) and \
            rule.notify_channels and await ns.is_pending(db, existing[0]) and \
            await _trigger_allowed(db, rule, r.key, now) and await ns.claim_pending(db, existing[0]):
        # Quiet hours held the trigger and the condition still breaches: send it now.
        await db.commit()
        sent = await _notify(db, rule, metric, r, condition, event_sentence=sentence(metric, r, condition) + ".",
                             alert_id=existing[0], first_seen=existing[1], now=now, checked=True)
        await ns.stamp(db, existing[0], sent)
    await db.commit()


# ─── Pass ────────────────────────────────────────────────────────────────────

_RULE_SQL = """
SELECT id, name, metric, operator, threshold, conditions, severity, min_duration, clear_duration,
       notify_channels, cooldown, device_id, group_id, device_type, location, scope_tag, profile_id,
       service_check_id, service_check_group_id, recovery_alert,
       email_subject, email_body, sms_template, recovery_email_subject, recovery_email_body,
       recovery_sms_template, schedule_start, schedule_end, schedule_days, {prio}
FROM alert_rules
WHERE enabled = true AND metric IN ('service_tls_days', 'service_response_ms', 'f5_cert_days')
"""


async def load_rules(db: AsyncSession) -> list:
    prio = await funnel.rule_priority_column(db)
    return (await db.execute(text(_RULE_SQL.format(prio=prio)))).all()


async def evaluate_service_metric_rules(db: AsyncSession, *, now: Optional[datetime] = None,
                                        response_fetcher: Optional[Callable] = None) -> dict[str, int]:
    """One pass over every enabled certificate-expiry / response-time rule.

    ``now`` is the evaluation instant; ``response_fetcher(check_intervals,
    window_s, now)`` replaces the ClickHouse reader in tests."""
    now = now or datetime.now(timezone.utc)
    counts = {"rules": 0, "raised": 0, "resolved": 0}
    rules = await load_rules(db)
    counts["rules"] = len(rules)
    if not rules:
        return counts
    metrics = {r.metric for r in rules}
    checks: dict[str, dict] = {}
    in_maintenance: set[str] = set()
    if metrics & SERVICE_METRICS:
        checks = await load_service_checks(db)
        from app.services.service_maintenance import active_check_ids
        in_maintenance = {str(i) for i in await active_check_ids(db, now)}
    devices: dict[str, dict] = {}
    certs: list[dict] = []
    excluded = family = None
    if F5_CERT_DAYS in metrics:
        devices, certs = await load_f5_certificates(db)
        from app.services import alert_rule_exclusions
        from app.services.device_templates import family as tfamily
        excluded = await alert_rule_exclusions.load(db)
        family = await tfamily.load(db)
    from app.api.v1.alert_engine import _device_in_maintenance
    device_maintenance: dict[str, bool] = {}
    response_cache: dict[int, Optional[dict]] = {}
    fetch = response_fetcher or fetch_response_stats
    # Every check some response-time rule covers, read once per averaging window.
    response_checks = {c["id"]: int(c["check_interval"] or 60) for c in checks.values()
                       if c["id"] not in in_maintenance
                       and any(r.metric == RESPONSE_MS and service_rule_covers(r, c) for r in rules)}

    for rule in rules:
        condition = rule_condition(rule)
        metric = condition.get("metric") or rule.metric
        if metric != rule.metric or metric not in METRICS:
            logger.error("Service metric rule %s skipped: conditions must use one metric", rule.id)
            continue
        readings: list[Reading] = []
        if metric in SERVICE_METRICS:
            scoped = [c for c in checks.values() if service_rule_covers(rule, c)
                      and c["id"] not in in_maintenance]
            window = response_window_s(getattr(rule, "min_duration", 0))
            stats: Optional[dict] = {}
            if metric == RESPONSE_MS and scoped:
                if window not in response_cache:
                    try:
                        response_cache[window] = await asyncio.to_thread(
                            fetch, response_checks, window, now)
                    except Exception:
                        logger.exception("Response times unavailable; open incidents are preserved")
                        response_cache[window] = None
                stats = response_cache[window]
                if stats is None:
                    continue
            for c in scoped:
                ctx = {"check_name": c["name"], "check_type": c["check_type"],
                       "target": c["target_url"] or c["target_host"]}
                base = dict(key=f"check:{c['id']}", asset=check_asset(c),
                            hostname=c["device_hostname"], ip=c["device_ip"],
                            device_id=c["device_id"], service_check_id=c["id"], context=ctx)
                if metric == TLS_DAYS:
                    days, expiry = tls_reading(c, now)
                    readings.append(Reading(value=days, expires_on=expiry, cert=check_cert_name(c), **base))
                else:
                    ms, win = response_value(stats.get(c["id"]), c, now, window)
                    readings.append(Reading(value=ms, window_s=win, **base))
        else:
            for cert in certs:
                did = cert["device_id"]
                dev = devices[did]
                if dev.get("status") == "maintenance" or not device_rule_covers(rule, did, dev, family):
                    continue
                if excluded is not None and excluded.device(rule.id, did):
                    continue
                if did not in device_maintenance:
                    device_maintenance[did] = await _device_in_maintenance(db, did)
                if device_maintenance[did]:
                    continue
                days, expiry = f5_reading(cert, now)
                name = cert["label"] or cert["instance"]
                readings.append(Reading(
                    key=f"device:{did}:{cert['instance']}", value=days, asset=f5_asset(dev),
                    hostname=dev["hostname"], ip=dev["ip"], device_id=did, dimension=cert["instance"],
                    cert=name, expires_on=expiry, context={"target": name}))
        for r in readings:
            try:
                await apply_reading(db, rule, metric, r, condition, now, counts)
            except Exception:
                logger.exception("Service metric rule %s failed for %s", rule.id, r.key)
                await db.rollback()
    if counts["raised"] or counts["resolved"]:
        logger.info("service metric alert eval: %d rules, %d raised, %d resolved",
                    counts["rules"], counts["raised"], counts["resolved"])
    return counts


async def run_service_metric_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import SERVICE_METRIC_ALERT_PASS, run_pass
    return await run_pass(SERVICE_METRIC_ALERT_PASS, evaluate_service_metric_rules, session_factory)


async def service_metric_alert_evaluator_loop() -> None:
    await asyncio.sleep(35)  # let the app boot and the first check results land
    while True:
        try:
            await run_service_metric_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("service metric alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
