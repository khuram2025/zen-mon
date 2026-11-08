"""Network-device (SNMP) alert evaluation.

Ping/trap alerting is event-driven (the Go poller pushes status changes to
alert_engine); host metrics use a periodic ClickHouse evaluator
(host_alert_service). Polled SNMP metrics — interface utilization/errors/
oper-status and device cpu/memory/temperature — also arrive continuously in
ClickHouse, so they get the same periodic treatment here.

Every ``EVAL_INTERVAL_S`` this loads the enabled alert rules whose ``metric`` is
one of the network keys, computes the current value for every in-scope device
(and interface, for per-interface metrics), and raises/resolves a device-scoped
row in ``alerts``. Rules dedupe on (rule_id, device_id, if_index): an alert is
only raised when no matching active alert already exists. Transaction locks
serialize rule/entity evaluation, the whole pass runs in one API worker at a
time (``alert_worker_lock``), and a trigger in flight is marked "sending" so
no other pass announces it again; deliveries are keyed per incident event,
channel and recipient (``dispatch_to_channels``).

An open incident resolves only after its cleared condition held for the rule's
``clear_duration`` (NULL = 300 s, 0 = immediate) against its reset threshold —
the explicit one, or for device CPU/memory 2 points on the healthy side of the
trigger (``network_conditions``). Missing data never counts as clear time.

Scope: a rule with no device_id/group_id/device_type/location applies to every
SNMP-monitored device; otherwise the usual scope filters apply. For interface
metrics, the optional ``target`` column narrows to interfaces whose
name/descr/alias contains it (case-insensitive) or whose if_index matches
exactly; empty target = all monitored interfaces.

Unsupported canonical state keys are rejected at rule authoring; collected
template state series are evaluated independently per component.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap
from app.services import network_history as _nh
from app.services.host_alert_service import dispatch_to_channels
from app.services import alert_funnel as funnel
from app.services.tag_service import tag_set as _tag_set
from app.services import alert_notify_state as ns

logger = logging.getLogger("zenplus.network_alerts")

EVAL_INTERVAL_S = 60
DEFAULT_WINDOW_S = 120       # sustained-window when a rule has no min_duration
UPTIME_LOOKBACK_S = 900      # how far back to look for a sysUpTime reset

# Per-interface metrics (computed from snmp_if_metrics, joined to interface speed).
INTERFACE_METRICS = {
    "if_in_bps", "if_out_bps", "if_util_pct",
    "if_errors", "if_discards", "if_oper_status",
}
# Device scalar metrics (from snmp_metrics, keyed by metric_key).
SCALAR_METRICS = {"cpu", "memory", "temperature", "session_count"}
SPECIAL_METRICS = {"uptime_reset"}
NETWORK_METRICS = INTERFACE_METRICS | SCALAR_METRICS | SPECIAL_METRICS

_OPS = {
    "gt": lambda a, b: a > b, ">": lambda a, b: a > b,
    "gte": lambda a, b: a >= b, ">=": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "<": lambda a, b: a < b,
    "lte": lambda a, b: a <= b, "<=": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "==": lambda a, b: a == b,
    "neq": lambda a, b: a != b, "!=": lambda a, b: a != b,
}

# oper_status code that means the interface is down (IF-MIB ifOperStatus).
_OPER_DOWN = 2


def _cmp(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get((operator or "").strip())
    return bool(fn and fn(value, threshold))


def _since(window_s: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")


# SQL that is true for a sample which does NOT breach — the negation of each
# operator. Used to find the most recent healthy sample, which is what dates
# the start of a breach.
_NOT_BREACH_SQL = {
    "gt": "value <= %(t)s", ">": "value <= %(t)s",
    "gte": "value < %(t)s", ">=": "value < %(t)s",
    "lt": "value >= %(t)s", "<": "value >= %(t)s",
    "lte": "value > %(t)s", "<=": "value > %(t)s",
    "eq": "value != %(t)s", "==": "value != %(t)s",
    "neq": "value = %(t)s", "!=": "value = %(t)s",
}


# ─── ClickHouse fetchers ────────────────────────────────────────────────────

def _if_fleet(window_s: int) -> dict[tuple[str, int], dict]:
    """{(device_id, if_index): {avg_in, avg_out, peak, in_err, out_err, ...}}."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id, if_index,
                   avg(in_bps), avg(out_bps),
                   max(greatest(in_bps, out_bps)),
                   max(in_errors) - min(in_errors),
                   max(out_errors) - min(out_errors),
                   max(in_discards) - min(in_discards),
                   max(out_discards) - min(out_discards),
                   argMax(oper_status, timestamp)
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(s)s
            GROUP BY device_id, if_index
            """,
            parameters={"s": _since(window_s)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse if-metrics query failed: %s", exc)
        return {}

    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        out[(str(r[0]), int(r[1]))] = {
            "avg_in": float(r[2] or 0), "avg_out": float(r[3] or 0),
            "peak": float(r[4] or 0),
            "errors": float((r[5] or 0) + (r[6] or 0)),
            "discards": float((r[7] or 0) + (r[8] or 0)),
            "oper": int(r[9] or 0),
        }
    return out


def _scalar_hold(metric: str, operator: str, threshold: float,
                 min_duration: int) -> dict[str, dict]:
    """{device_id: {"value": latest, "held_s": seconds the breach has lasted}}.

    "Condition must exist for N seconds" means exactly that: continuously true
    for N seconds. Averaging the window instead (the previous behaviour) let a
    single high sample drag the mean over the threshold and fire long before N
    elapsed — with ~90s polling and a 300s hold, a spike from 89% to 100% fired
    on the very first breaching poll.

    So instead of a mean, this dates the breach from the most recent healthy
    sample and lets the caller compare that age against min_duration. The
    lookback runs well past the hold time so that sample is still visible; when
    every sample in it breaches, the breach is at least as old as the oldest
    sample we can see.
    """
    from app.core.database import get_clickhouse_client

    not_breach = _NOT_BREACH_SQL.get((operator or "").strip())
    if not not_breach:
        return {}
    # Far enough back to still see the last healthy sample, with a floor so
    # short holds keep enough history to be meaningful.
    lookback = max(min_duration * 3, 900)

    try:
        client = get_clickhouse_client()
        rows = client.query(
            f"""
            SELECT device_id,
                   argMax(value, timestamp) AS last_v,
                   countIf({not_breach}) AS n_ok,
                   dateDiff('second',
                            if(countIf({not_breach}) = 0,
                               min(timestamp),
                               maxIf(timestamp, {not_breach})),
                            now()) AS held_s
            FROM zenplus.snmp_metrics
            WHERE metric_key = %(m)s AND timestamp >= %(s)s
            GROUP BY device_id
            """,
            parameters={"m": metric, "t": float(threshold), "s": _since(lookback)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse hold query failed (%s): %s", metric, exc)
        return {}

    return {
        str(r[0]): {"value": float(r[1] or 0), "held_s": int(r[3] or 0)}
        for r in rows
    }


def _eval_scalar_hold(rule, entry: dict | None) -> tuple[bool, float, str] | None:
    """Breach only once the condition has genuinely held for min_duration."""
    if not entry:
        return None
    value = entry["value"]
    breaching = _cmp(value, rule.operator, float(rule.threshold or 0))
    if not breaching:
        return False, value, ap.format_value(rule.metric, value)
    hold = int(rule.min_duration or 0)
    if entry["held_s"] < hold:
        # Breaching, but not for long enough yet. Reported as not-breaching so
        # an already-open alert still resolves when the condition clears.
        return False, value, ap.format_value(rule.metric, value)
    detail = ap.format_value(rule.metric, value)
    if hold:
        detail += f" for {ap.humanize_duration(entry['held_s'])}"
    return True, value, detail


def _tpl_fleet(window_s: int) -> dict[str, dict[str, float]]:
    """{device_id: {series_key: latest_value}} for monitoring-template metrics
    (series keys 'tpl_*', written by the poller's template collector)."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id, metric_key, argMax(value, timestamp)
            FROM zenplus.snmp_metrics
            WHERE metric_key LIKE 'tpl\\_%%' AND timestamp >= %(s)s
            GROUP BY device_id, metric_key
            """,
            parameters={"s": _since(window_s)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse template query failed: %s", exc)
        return {}

    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(str(r[0]), {})[str(r[1])] = float(r[2] or 0)
    return out


def _eval_template(rule, series: dict[str, float]) -> tuple[bool, float, str] | None:
    """Evaluate a tpl_* rule for one device. A rule on 'tpl_fgt_tun_status'
    matches the scalar series of that key AND every per-row instance series
    ('tpl_fgt_tun_status_<inst>'); the rule breaches when ANY matching series
    does — e.g. 'any tunnel down', 'any AP above 90% CPU'."""
    key = rule.metric
    matches = {k: v for k, v in series.items() if k == key or k.startswith(key + "_")}
    if not matches:
        return None
    op, thr = rule.operator, float(rule.threshold or 0)
    breaching = {k: v for k, v in matches.items() if _cmp(v, op, thr)}
    if breaching:
        pick = max if (op or "").strip() in ("gt", ">", "gte", ">=") else min
        worst = pick(breaching.values())
        return True, worst, f"{len(breaching)}/{len(matches)} series breach (worst {worst:g})"
    return False, next(iter(matches.values())), f"0/{len(matches)} series breach"


def _uptime_resets() -> set[str]:
    """device_ids whose sysUpTime dropped within the lookback window (reboot)."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id, max(value), argMax(value, timestamp)
            FROM zenplus.snmp_metrics
            WHERE metric_key = 'uptime' AND timestamp >= %(s)s
            GROUP BY device_id
            """,
            parameters={"s": _since(UPTIME_LOOKBACK_S)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse uptime query failed: %s", exc)
        return set()

    # latest sample materially lower than an earlier one ⇒ the counter reset.
    return {str(r[0]) for r in rows if (float(r[1] or 0) - float(r[2] or 0)) > 60}


# ─── Postgres fetchers ──────────────────────────────────────────────────────

async def _snmp_devices(db: AsyncSession) -> dict[str, dict]:
    """{device_id: {hostname, ip, device_type, location, group_id, tags}} for SNMP devices.

    Controller-managed children (poll_mode via_controller) are included: the
    managed-device sync writes their controller-reported cpu/memory/uptime to
    their own series, so a rule's breach belongs to the AP or switch, not to
    its FortiGate. Their cadence is the controller's SNMP interval."""
    rows = (await db.execute(text(
        "SELECT d.id, d.hostname, d.device_type, d.location, d.group_id, d.tags, "
        "COALESCE(c.snmp_poll_interval, d.snmp_poll_interval), p.oid_groups, "
        "COALESCE(host(d.ip_address), host(d.managed_ip)) "
        "FROM devices d LEFT JOIN device_profiles p ON p.id = d.profile_id "
        "LEFT JOIN devices c ON c.id = d.managed_by_device_id AND d.poll_mode = 'via_controller' "
        "WHERE (d.snmp_enabled = true OR d.poll_mode = 'via_controller') AND d.status <> 'maintenance' ORDER BY d.id"
    ))).all()
    return {
        str(r[0]): {
            "hostname": r[1] or "device",
            "device_type": r[2],
            "location": r[3],
            "group_id": str(r[4]) if r[4] else None,
            "tags": _tag_set(r[5]),
            "poll_interval": int(r[6] or 60),
            "metric_intervals": template_intervals(r[7], int(r[6] or 60)),
            "ip": r[8] or "",
        }
        for r in rows
    }


def template_intervals(groups, default):
    if isinstance(groups, str):
        groups = json.loads(groups)
    return {'tpl_' + m['key']: max(default, int(g.get('interval_seconds') or default))
            for g in groups or [] for m in g.get('metrics', [])}


def freshness_budgets(dev, metrics):
    return {m: max(180, 3 * max([dev['poll_interval']] + [interval for root, interval in dev.get('metric_intervals', {}).items()
                                                        if m == root or m.startswith(root + '_')])) for m in metrics}


_warned_missing_last_up = False


async def _interfaces(db: AsyncSession) -> dict[str, list[dict]]:
    """{device_id: [interface dicts]} for monitored interfaces with a known speed.

    ``ever_up`` is false for a port the poller has never seen oper-up (an
    unused access port): its oper-down state is its normal state, not a fault.
    """
    global _warned_missing_last_up
    sql = ("SELECT device_id, if_index, if_name, if_descr, if_alias, "
           "COALESCE(configured_speed_bps, if_speed) AS speed, admin_status, monitored, "
           "{ever_up} FROM device_interfaces WHERE monitored = true")
    try:
        rows = (await db.execute(text(sql.format(ever_up="last_up_at IS NOT NULL")))).all()
    except Exception as exc:
        # Rolling upgrade: code deployed before migrate-126 ran. Degrade to
        # the previous behaviour (every port eligible) with one warning.
        if "last_up_at" not in str(exc):
            raise
        await db.rollback()
        if not _warned_missing_last_up:
            logger.warning("interface-down baseline disabled: device_interfaces.last_up_at "
                           "missing — run migrate-126-device-identity-and-interface-baseline.sql")
            _warned_missing_last_up = True
        rows = (await db.execute(text(sql.format(ever_up="true")))).all()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append({
            "if_index": int(r[1]),
            "if_name": r[2] or "", "if_descr": r[3] or "", "if_alias": r[4] or "",
            "speed": int(r[5]) if r[5] else 0,
            "admin_up": (str(r[6] or "").lower() == "up"),
            "ever_up": bool(r[8]),
        })
    return out


# ─── Scope / target matching ────────────────────────────────────────────────

def _device_in_scope(rule, dev_id: str, dev: dict) -> bool:
    if rule.device_id and str(rule.device_id) != dev_id:
        return False
    if rule.group_id and (dev["group_id"] != str(rule.group_id)):
        return False
    if rule.device_type and rule.device_type != dev["device_type"]:
        return False
    if rule.location and (not dev["location"] or rule.location.lower() not in dev["location"].lower()):
        return False
    if rule.scope_tag and rule.scope_tag.strip().lower() not in dev.get("tags", set()):
        return False
    return True


def _iface_matches_target(iface: dict, target: str | None) -> bool:
    if not target:
        return True
    t = target.strip().lower()
    if t.isdigit() and int(t) == iface["if_index"]:
        return True
    return any(t in (iface[k] or "").lower() for k in ("if_name", "if_descr", "if_alias"))


def _iface_label(iface: dict) -> str:
    return iface["if_name"] or iface["if_descr"] or f"if {iface['if_index']}"


# ─── Alert raise / resolve (device-scoped rows in `alerts`) ──────────────────

async def _active_alert(db: AsyncSession, rule_id, device_id: str, if_index):
    """The open alert for this rule/device/interface: (id, triggered_at) or None."""
    row = (await db.execute(text(
        "SELECT id, triggered_at FROM alerts "
        "WHERE rule_id = :rid AND device_id = :did AND status IN ('active','acknowledged') "
        "  AND COALESCE(metadata->>'if_index','') = :ifx "
        "ORDER BY triggered_at DESC LIMIT 1"
    ), {"rid": str(rule_id), "did": device_id, "ifx": "" if if_index is None else str(if_index)})).first()
    return (row[0], row[1]) if row else None


async def _raise(db: AsyncSession, rule, device_id: str, message: str,
                 value: float, if_index, extra: dict):
    now = datetime.now(timezone.utc)
    # The trigger is "sending" until _apply stamps its outcome: neither pending
    # nor notified, so another pass never announces it a second time.
    meta = {"rule_id": str(rule.id), "metric": rule.metric,
            "value": round(value, 2), "threshold": float(rule.threshold or 0),
            **ns.sending_metadata()}
    if if_index is not None:
        meta["if_index"] = str(if_index)
    meta.update(extra)
    # The incident's priority is fixed when it is raised (migrate-164).
    p_col, p_val, p_params = funnel.priority_insert(
        await funnel.priority_supported(db), getattr(rule, "priority", None),
        rule.severity or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata{p_col}) "
        f"VALUES (:did, :rid, 'active', :sev, :msg, :ts, CAST(:meta AS jsonb){p_val}) "
        "RETURNING id"
    ), {"did": device_id, "rid": str(rule.id), "sev": rule.severity or "warning",
        "msg": message, "ts": now, "meta": json.dumps(meta), **p_params})).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id, "m": json.dumps({"resolved_by": "network_evaluator",
                                                     "resolved_at": now.isoformat()})})
    return res.rowcount or 0


# ─── Per-metric value resolution ─────────────────────────────────────────────

def _eval_interface(rule, m: dict, iface: dict) -> tuple[bool, float, str] | None:
    """(breach, value, detail) for an interface metric, or None to skip."""
    metric, op, thr = rule.metric, rule.operator, float(rule.threshold or 0)
    if metric == "if_in_bps":
        v = m["avg_in"]; return _cmp(v, op, thr), v, f"{v/1e6:.1f} Mbps in"
    if metric == "if_out_bps":
        v = m["avg_out"]; return _cmp(v, op, thr), v, f"{v/1e6:.1f} Mbps out"
    if metric == "if_util_pct":
        if not iface["speed"]:
            return None  # cannot compute utilization without a link speed
        v = min(100.0, m["peak"] / iface["speed"] * 100.0)
        return _cmp(v, op, thr), v, f"{v:.0f}% of {iface['speed']/1e6:.0f}Mb"
    if metric == "if_errors":
        v = m["errors"]; return _cmp(v, op, thr), v, f"{int(v)} errors"
    if metric == "if_discards":
        v = m["discards"]; return _cmp(v, op, thr), v, f"{int(v)} discards"
    if metric == "if_oper_status":
        # Only alert on interfaces that are administratively up and have been
        # up at least once — an admin-down or never-used port reporting
        # oper-down is its normal state, not a fault.
        if not iface["admin_up"] or not iface.get("ever_up", True):
            return None
        v = float(m["oper"])
        return _cmp(v, op, thr), v, ("down" if int(v) == _OPER_DOWN else f"oper={int(v)}")
    return None


def _message(rule, hostname: str, detail: str, iface_label: str | None,
             ip_address: str = "") -> str:
    where = ap.host_label(hostname, ip_address) + (f" / {iface_label}" if iface_label else "")
    return f"{rule.name}: {where} — {detail}"


async def _trigger_allowed(db: AsyncSession, rule, *, device_id=None, if_index=None) -> bool:
    """Whether a trigger may be sent now: outside the rule's cooldown and
    inside its schedule window. Recoveries never ask."""
    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    cooldown = int(getattr(rule, 'cooldown', 0) or 0)
    if device_id is not None and cooldown > 0:
        prior = (await db.execute(text("SELECT 1 FROM alerts WHERE rule_id = :rid AND device_id = :did "
            "AND COALESCE(metadata->>'if_index','') = :ifx AND metadata->>'notified' = 'true' "
            "AND triggered_at > now() - make_interval(secs => :seconds) LIMIT 1"),
            {'rid': str(rule.id), 'did': device_id, 'ifx': '' if if_index is None else str(if_index),
             'seconds': cooldown})).first()
        if prior:
            return False
    tz = await get_configured_timezone(db)
    return notifications_allowed(
        getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
        getattr(rule, "schedule_days", None), tz,
    )


async def _notify(db: AsyncSession, rule, hostname: str, *, reading: str = "",
                  iface_label: str | None = None, is_recovery: bool = False,
                  duration: str = "", device_id=None, if_index=None,
                  ip_address: str = "", value=None, value_context: dict | None = None,
                  alert_id=None, first_seen=None, event: str | None = None,
                  checked: bool = False, asset_type: str | None = None,
                  branding=None, context: dict | None = None,
                  lifecycle: str | None = None) -> bool:
    """Send one metric alert — or its all-clear. True when it was dispatched.

    ``alert_id`` + ``event`` key the delivery (one send per incident event,
    channel and recipient). ``checked`` means the caller already asked
    :func:`_trigger_allowed`. ``context`` is an event's own facts (a reboot:
    when, uptime now and before); ``lifecycle`` "changed" announces a further
    event on an open incident ("rebooted again").
    """
    # Quiet hours: the alert row is already recorded; only the outbound
    # notification is gated by the rule's schedule window. A recovery notice is
    # never suppressed, or an operator is left believing it is still breached.
    if not is_recovery and not checked and lifecycle is None and not await _trigger_allowed(
            db, rule, device_id=device_id, if_index=if_index):
        return False
    sev = (rule.severity or "warning")
    threshold = float(rule.threshold) if rule.threshold is not None else None
    event_metric = rule.metric in ap.EVENT_SENTENCE_METRICS
    if event_metric:
        # A reboot is an event: the 0/1 flag is not a reading anyone wants.
        reading, value = "", None
    elif value is not None and not reading:
        reading = ap.reading_text(rule.metric, value, rule.operator, threshold, value_context)
    now = datetime.now(timezone.utc)
    incident = ap.incident_context(alert_id=alert_id, first_seen=first_seen, now=now,
                                   lifecycle=lifecycle or ("recovered" if is_recovery else "new")) if alert_id else {}
    # An interface rule is about the port, not the chassis, so the sentence
    # names "core-router-01 (10.0.0.1) / port14" and the details table repeats it.
    label = ap.host_label(hostname, ip_address)
    v = ap.rule_phrasing(rule, hostname=f"{label} / {iface_label}" if iface_label else label,
                         is_recovery=is_recovery, reading=reading, duration=duration,
                         value=value, value_context=context if event_metric else value_context,
                         asset_type=asset_type, branding=branding, now=now)
    v.update({"rule_name": rule.name or "Alert",
              "hostname": f"{hostname} / {iface_label}" if iface_label else hostname,
              "ip_address": ip_address or "",
              "severity": sev.upper(),
              "status": "RESOLVED" if is_recovery else "ALERT",
              "timestamp": now.strftime("%H:%M UTC on %d %b %Y")})
    v.update(incident)
    prefix = 'recovery_' if is_recovery else ''
    body = _render(getattr(rule, prefix + 'email_body', None) or (ap.DEFAULT_RECOVERY_EMAIL_BODY if is_recovery else ap.DEFAULT_EMAIL_BODY), v)
    sms = _render(getattr(rule, 'recovery_sms_template' if is_recovery else 'sms_template', None) or (ap.DEFAULT_RECOVERY_SMS if is_recovery else ap.DEFAULT_SMS), v)
    subject = _render(getattr(rule, prefix + 'email_subject', None) or (ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if is_recovery else ap.DEFAULT_EMAIL_SUBJECT), v)
    sent = await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": subject,
        "body": body, "message": sms,
        "hostname": hostname, "ip_address": ip_address or "",
        "status": "RESOLVED" if is_recovery else "ALERT", "severity": sev,
        "resolved": is_recovery,
        "rule_name": rule.name,
        "headline_metric": (
            ap.headline_metric(rule.metric, value, rule.operator, threshold, context=value_context)
            if value is not None else {
                "label": ap.metric_noun(rule.metric), "value": reading,
                "secondary_label": "Threshold", "secondary_value": v.get("threshold_value"),
            }) if reading else None,
        "details": [("Alert rule", rule.name),
                    ("Interface", iface_label),
                    ("Condition", v.get("condition_label")),
                    ("Active for", duration if is_recovery and not event_metric else None)],
        "triggered_at": now.isoformat(),
        "rule_id": str(rule.id),
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": event or ("recovery" if is_recovery else "trigger"),
        "is_recovery": is_recovery,
        **funnel.notification_fields(sev, getattr(rule, "priority", None), trigger=not is_recovery),
        **incident,
    })
    return bool(sent)


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


# ─── Main evaluation pass ────────────────────────────────────────────────────

_warned_missing_clear_duration = False


async def _load_rules(db: AsyncSession) -> list:
    """Enabled network rules. Before migrate-162 the clear hold is the default."""
    global _warned_missing_clear_duration
    metric_list = ",".join(f"'{m}'" for m in sorted(NETWORK_METRICS))
    prio = await funnel.rule_priority_column(db)

    def sql(clear: str):
        return text(
            f"SELECT id, name, metric, operator, threshold, severity, min_duration, {clear}, "
            f"notify_channels, cooldown, device_id, group_id, device_type, location, scope_tag, target, "
            f"recovery_alert, conditions, condition_logic, "
            f"email_subject, email_body, sms_template, recovery_email_subject, recovery_email_body, recovery_sms_template, "
            f"schedule_start, schedule_end, schedule_days, {prio} "
            f"FROM alert_rules WHERE enabled = true "
            f"AND (metric IN ({metric_list}) OR metric LIKE 'tpl\\_%')")
    try:
        return (await db.execute(sql("clear_duration"))).all()
    except Exception as exc:
        if "clear_duration" not in str(exc):
            raise
        await db.rollback()
        if not _warned_missing_clear_duration:
            logger.warning("alert_rules.clear_duration missing — run migrate-162; "
                           "using the default clear hold")
            _warned_missing_clear_duration = True
        return (await db.execute(sql("NULL AS clear_duration"))).all()


async def evaluate_network_rules(db: AsyncSession, *, now: float | None = None) -> dict[str, int]:
    """One pass over every enabled network rule. ``now`` (epoch seconds) is
    the evaluation instant; tests replay history with it."""
    rules = await _load_rules(db)
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    devices = await _snmp_devices(db)
    if not devices:
        return {"rules": len(rules), "raised": 0, "resolved": 0}
    interfaces = await _interfaces(db)

    # Active device-scoped silences (snoozes), loaded once per pass. Keys match
    # the dedupe written by the snooze endpoint: rule:<id>[:if:<idx>].
    silences: set[tuple[str, str]] = {
        (r.did, r.dedupe) for r in (await db.execute(text(
            "SELECT device_id::text AS did, dedupe FROM alert_silences "
            "WHERE device_id IS NOT NULL AND (until IS NULL OR until > NOW())"
        ))).all()
    }

    from app.services.network_conditions import clear_hold_seconds, evaluate, validate_conditions
    from app.services.network_history import fetch_history, entities
    from app.api.v1.alert_engine import _find_suppressing_dependency, _device_in_maintenance
    from app.services.regional_time import notification_branding
    branding = await notification_branding(db)

    raised = resolved = 0
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    dependency_cache = {}
    maintenance_cache = {}
    history_cache = {}
    for rule in rules:
        conditions = rule.conditions
        if isinstance(conditions, str):
            conditions = json.loads(conditions)
        conditions = conditions or [dict(metric=rule.metric, operator=rule.operator,
                                         threshold=float(rule.threshold or 0))]
        try:
            validate_conditions(conditions)
        except ValueError as exc:
            logger.error("Network rule %s skipped: %s", rule.id, exc)
            continue
        metrics = tuple(sorted({c['metric'] for c in conditions}))
        reboot_rule = metrics == ('uptime_reset',)
        hold = 0 if reboot_rule else int(rule.min_duration or 0)
        clear_hold = (event_hold_seconds(getattr(rule, 'clear_duration', None)) if reboot_rule
                      else clear_hold_seconds(getattr(rule, 'clear_duration', None)))
        # Include enough context for the first breach (or the whole clear
        # hold) and freshness expiry.
        max_gap = max(max(freshness_budgets(d, metrics).values()) for d in devices.values())
        lookback = max(max(hold, clear_hold) + 2 * max_gap, 900)
        cache_key = (metrics, lookback)
        if cache_key not in history_cache:
            try:
                history_cache[cache_key] = await asyncio.to_thread(
                    fetch_history, metrics, now - lookback, now)
            except Exception:
                logger.exception("Network rule %s history unavailable; preserving alerts", rule.id)
                continue
        scalar, if_data = history_cache[cache_key]
        for did, dev in devices.items():
            if not _device_in_scope(rule, did, dev):
                continue
            if did not in maintenance_cache:
                maintenance_cache[did] = await _device_in_maintenance(db, did)
            if maintenance_cache[did]:
                continue
            if did not in dependency_cache:
                dependency_cache[did] = await _find_suppressing_dependency(db, did)
            if dependency_cache[did]:
                # Missing downstream data under a failed dependency must not
                # create or clear child incidents. Store the suppression reason
                # on existing rows so the operator can inspect it.
                await db.execute(text(
                    "UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb) || "
                    "CAST(:meta AS jsonb) WHERE device_id = :did AND rule_id = :rid "
                    "AND status IN ('active','acknowledged')"
                ), {'did': did, 'rid': str(rule.id), 'meta': json.dumps({
                    'suppressed_by_dependency': True,
                    'parent_device_id': str(dependency_cache[did]['parent_device_id'])})})
                continue
            await db.execute(text(
                "UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb) - 'parent_device_id' "
                "|| jsonb_build_object('suppressed_by_dependency', false) "
                "WHERE device_id = :did AND rule_id = :rid AND status IN ('active','acknowledged') "
                "AND metadata->>'suppressed_by_dependency' = 'true'"
            ), {'did': did, 'rid': str(rule.id)})
            if reboot_rule:
                counts = await _evaluate_reboot(
                    db, rule, did, dev, scalar.get(did, {}), now=now, hold_s=clear_hold,
                    max_gap=freshness_budgets(dev, ('uptime',))['uptime'],
                    silences=silences, branding=branding)
                raised += counts[0]
                resolved += counts[1]
                await db.commit()
                continue
            device_ifs = {idx: series for (device, idx), series in if_data.items() if device == did}
            bound_entities = list(entities(
                    conditions, scalar.get(did, {}), device_ifs, interfaces.get(did, []),
                    lambda iface: _iface_matches_target(iface, rule.target)))
            # Keep pre-upgrade aggregate template incidents on their original
            # identity until observed recovery; new incidents are per component.
            if any(m.startswith('tpl_') for m in metrics):
                await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                 {'key': f'network:{rule.id}:{did}:None'})
                legacy = await _active_alert(db, rule.id, did, None)
                if legacy and bound_entities:
                    from app.services.network_conditions import combine
                    legacy_results = [evaluate(conditions, rule.condition_logic or 'AND', h,
                                      now=now, max_gap=freshness_budgets(dev, metrics), active=True,
                                      clear_hold=clear_hold)
                                      for _, _, h, _ in bound_entities]
                    state = combine([r.breach for r in legacy_results], 'OR')
                    if state is not None:
                        raised, resolved = await _apply(db, rule, did, dev['hostname'], None,
                            None, state, 0, 'Legacy component aggregate',
                            {'legacy_aggregate': True}, raised, resolved, silences,
                            ip_address=dev.get('ip', ''))
            for identity, label, history, extra in bound_entities:
                await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                                 {'key': f'network:{rule.id}:{did}:{identity}'})
                existing = await _active_alert(db, rule.id, did, identity)
                result = evaluate(conditions, rule.condition_logic or 'AND', history,
                                  now=now, hold=hold, max_gap=freshness_budgets(dev, metrics),
                                  active=existing is not None, clear_hold=clear_hold)
                if result.breach is None:
                    if existing:
                        await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                                              "|| CAST(:meta AS jsonb) WHERE id = :id"),
                                         {'id': existing[0], 'meta': json.dumps({
                                             'data_state': result.reason,
                                             'cleared_seconds': round(result.cleared_seconds)})})
                    # Unknown, pending hold or pending clear is neither a
                    # trigger nor a recovery: an open incident just continues.
                    continue
                value = result.values.get(rule.metric)
                if value is None:
                    value = next((v for v in result.values.values() if v is not None), 0)
                context = _counter_context(history, rule.metric, now)
                detail = _detail_text(rule, conditions, result.values, context)
                extra.update({'held_seconds': result.held_seconds, 'values': result.values,
                              'data_state': 'current',
                              'suppressed_by_dependency': False})
                # Serialize this rule/entity across API workers. _apply commits
                # the inserted/resolved row before releasing the transaction lock.
                raised, resolved = await _apply(db, rule, did, dev['hostname'], identity,
                    label, result.breach, value, detail, extra, raised, resolved, silences,
                    ip_address=dev.get('ip', ''), value_context=context,
                    asset_type=ap.asset_type_label(dev.get('device_type')), branding=branding)
                await db.commit()
        await db.commit()

    if raised or resolved:
        logger.info("network alert eval: %d rules, %d raised, %d resolved", len(rules), raised, resolved)
    return {"rules": len(rules), "raised": raised, "resolved": resolved}


def _detail_text(rule, conditions, values: dict, context: dict | None = None) -> str:
    """What was measured, in words, for the stored alert message:
    "Device CPU 97% (limit 90%)", "the interface is down (link not
    operational)" — never "cpu=97" or a 0/1 flag."""
    parts = []
    for c in conditions:
        m = c.get('metric')
        v = values.get(m)
        if 'state' in ap.METRICS.get(m, {}):
            parts.append(ap.condition_phrase(m, c.get('operator'), c.get('threshold'), tense='past'))
        elif v is None:
            parts.append(f"{ap.metric_noun(m)} unknown (no current reading)")
        else:
            shown = ap.reading_text(m, v, c.get('operator'), c.get('threshold'),
                                    context if m == rule.metric else None)
            limit = ap.format_threshold(m, c.get('threshold'), c.get('operator'), v)
            parts.append(f"{ap.metric_noun(m)} {shown} (limit {limit})")
    text_ = '; '.join(p for p in parts if p)
    return text_[:1].upper() + text_[1:]


# ─── Reboots (sysUpTime went back) ───────────────────────────────────────────
#
# A reboot is an event, not a level. The stored rule is `uptime_reset == 1`
# (a 0/1 flag per poll), so its operator, threshold, reset level and breach
# hold mean nothing: the flag is 1 for exactly one poll. Instead:
#
# * A reboot is a poll whose uptime is more than REBOOT_MIN_DROP_S below the
#   previous one (not a 32-bit TimeTicks wrap at ~497 days). Its boot time is
#   that poll's time minus the new uptime.
# * It raises one incident per rule + device. A further reboot while it is
#   open updates the same incident ("rebooted again", one notification per
#   reboot) and restarts the hold; it never opens a second incident.
# * The incident closes itself once no further reboot was seen for the hold
#   (``clear_duration``; NULL = EVENT_HOLD_DEFAULT_S), or when an operator
#   resolves it. The all-clear says how long the device has stayed up, or
#   honestly that there is no current uptime reading.
# * A reboot already announced (open or resolved incident) is never paged
#   twice, so a manual resolve inside the hold stays resolved.

EVENT_HOLD_DEFAULT_S = 900
REBOOT_MIN_DROP_S = 60
# sysUpTime is TimeTicks (1/100 s, 32 bits): it wraps after ~497.1 days.
_UPTIME_WRAP_S = 2 ** 32 / 100.0


def event_hold_seconds(clear_duration) -> int:
    """How long an event incident (reboot) stays open after the last event."""
    if clear_duration is None:
        return EVENT_HOLD_DEFAULT_S
    return max(0, int(clear_duration))


def reboot_events(series) -> list[dict]:
    """Reboots in an uptime series [(epoch s, uptime s)], oldest first."""
    out: list[dict] = []
    prev = None
    for stamp, value in sorted((float(t), v) for t, v in series or [] if v is not None):
        value = float(value)
        if prev is not None and value < prev[1] - REBOOT_MIN_DROP_S:
            wrapped = prev[1] + (stamp - prev[0]) >= _UPTIME_WRAP_S - 3600
            if not wrapped:
                out.append({"detected_at": stamp, "rebooted_at": stamp - value,
                            "uptime_s": value, "previous_uptime_s": prev[1]})
        prev = (stamp, value)
    return out


async def _reboot_incident(db: AsyncSession, rule_id, device_id: str):
    """(open incident id, triggered_at, metadata) and the newest reboot this
    rule already announced for the device (epoch s; open or resolved)."""
    rows = (await db.execute(text(
        "SELECT id, triggered_at, status, metadata FROM alerts "
        "WHERE rule_id = :rid AND device_id = :did "
        "ORDER BY triggered_at DESC LIMIT 20"
    ), {"rid": str(rule_id), "did": device_id})).all()
    known = 0.0
    open_row = None
    for r in rows:
        meta = r[3] if isinstance(r[3], dict) else json.loads(r[3] or "{}")
        seen = meta.get("reboot_detected_at")
        stamp = r[1].timestamp() if r[1] is not None else 0.0
        # Before 1.25.3 a reboot incident carried no detection time; its
        # trigger time is at or after the reboot it announced.
        known = max(known, float(seen) if seen is not None else stamp)
        if open_row is None and r[2] in ("active", "acknowledged"):
            open_row = (r[0], r[1], meta)
    return open_row, known


async def _evaluate_reboot(db: AsyncSession, rule, did: str, dev: dict, series: dict, *,
                           now: float, hold_s: int, max_gap: float,
                           silences: set | None, branding) -> tuple[int, int]:
    """Raise, extend or close one rule/device reboot incident. (raised, resolved)."""
    from app.services.alert_schedule import notifications_allowed, get_configured_timezone

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {'key': f'network:{rule.id}:{did}:None'})
    uptime = [(float(t), v) for t, v in series.get('uptime', []) if v is not None and float(t) <= now]
    events = reboot_events(uptime)
    latest_sample = max(uptime, default=None)
    current = None
    if latest_sample and now - latest_sample[0] < max_gap:
        current = float(latest_sample[1]) + (now - latest_sample[0])
    opened, known = await _reboot_incident(db, rule.id, did)
    fresh = [e for e in events if e["detected_at"] > known + 1e-6]
    asset_type = ap.asset_type_label(dev.get('device_type'))
    ip = dev.get('ip', '')
    who = ap.asset_label(dev['hostname'], ip, asset_type)
    common = dict(device_id=did, ip_address=ip, asset_type=asset_type, branding=branding)

    if opened is None:
        recent = [e for e in fresh if now - e["detected_at"] < max(hold_s, 60)]
        if not recent:
            return 0, 0
        if silences and (did, f"rule:{rule.id}") in silences:
            return 0, 0
        first, last = recent[0], recent[-1]
        ctx = {"rebooted_at": last["rebooted_at"], "uptime_s": current if current is not None else last["uptime_s"],
               "previous_uptime_s": first["previous_uptime_s"], "reboot_count": len(recent),
               "first_rebooted_at": first["rebooted_at"]}
        message = ap.reboot_sentence(who, ctx, branding=branding, now=now)
        meta = {"reboot_detected_at": last["detected_at"], "rebooted_at": last["rebooted_at"],
                "first_rebooted_at": first["rebooted_at"], "reboot_count": len(recent),
                "previous_uptime_s": first["previous_uptime_s"], "hold_seconds": hold_s,
                "data_state": "current", "event": "reboot"}
        alert_id = await _raise(db, rule, did, message, 1.0, None, meta)
        if alert_id is None:
            return 0, 0
        await db.commit()
        sent = await _notify(db, rule, dev['hostname'], alert_id=alert_id,
                             first_seen=datetime.now(timezone.utc), context=ctx, **common)
        await ns.stamp(db, alert_id, sent)
        await db.commit()
        return 1, 0

    alert_id, started_at, meta = opened
    if fresh:
        # Rebooted again while the incident is open: same incident, new facts,
        # one notification per further reboot; the hold restarts.
        last = fresh[-1]
        count = int(meta.get("reboot_count") or 1) + len(fresh)
        ctx = {"rebooted_at": last["rebooted_at"], "uptime_s": current if current is not None else last["uptime_s"],
               "reboot_count": count,
               "first_rebooted_at": meta.get("first_rebooted_at") or meta.get("rebooted_at")
               or (started_at.timestamp() if started_at else None)}
        message = ap.reboot_sentence(who, ctx, branding=branding, now=now)
        await db.execute(text(
            "UPDATE alerts SET message = :msg, metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:meta AS jsonb) "
            "WHERE id = :id"), {"id": alert_id, "msg": message, "meta": json.dumps({
                "reboot_detected_at": last["detected_at"], "rebooted_at": last["rebooted_at"],
                "reboot_count": count, "hold_seconds": hold_s, "data_state": "current"})})
        await db.commit()
        tz = await get_configured_timezone(db)
        if (await ns.was_notified(db, alert_id) and notifications_allowed(
                getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
                getattr(rule, "schedule_days", None), tz)):
            await _notify(db, rule, dev['hostname'], alert_id=alert_id, first_seen=started_at,
                          context=ctx, lifecycle="changed",
                          event=f"reboot:{int(last['detected_at'])}", **common)
        return 0, 0

    last_at = float(meta.get("reboot_detected_at") or (started_at.timestamp() if started_at else now))
    if now - last_at < hold_s:
        if await ns.is_pending(db, alert_id) and rule.notify_channels and \
                await _trigger_allowed(db, rule, device_id=did) and await ns.claim_pending(db, alert_id):
            # Quiet hours held the trigger and the hold is still running.
            await db.commit()
            ctx = {"rebooted_at": meta.get("rebooted_at"), "uptime_s": current,
                   "previous_uptime_s": meta.get("previous_uptime_s"),
                   "reboot_count": meta.get("reboot_count") or 1,
                   "first_rebooted_at": meta.get("first_rebooted_at")}
            sent = await _notify(db, rule, dev['hostname'], alert_id=alert_id, first_seen=started_at,
                                 checked=True, context=ctx, **common)
            await ns.stamp(db, alert_id, sent)
            await db.commit()
        return 0, 0
    # No further reboot for the whole hold: the event is over.
    notified = await ns.was_notified(db, alert_id)
    n = await _resolve(db, alert_id)
    await db.commit()
    if n and notified and getattr(rule, "recovery_alert", True):
        ctx = {"rebooted_at": meta.get("rebooted_at"), "uptime_s": current,
               "reboot_count": meta.get("reboot_count") or 1, "hold_s": hold_s,
               "last_seen_at": latest_sample[0] if latest_sample else None}
        await _notify(db, rule, dev['hostname'], is_recovery=True,
                      duration=ap.duration_between(started_at), alert_id=alert_id,
                      first_seen=started_at, context=ctx, **common)
    return 0, n


def _counter_context(history, metric, now):
    """Interval and direction of a per-poll counter reading, when known."""
    fn = getattr(_nh, 'counter_context', None)
    try:
        return fn(history, metric, now) if fn else None
    except Exception:
        logger.debug("counter context unavailable for %s", metric, exc_info=True)
        return None


async def _apply(db, rule, device_id, hostname, if_index, iface_label,
                 breach, value, detail, extra, raised, resolved,
                 silences: set[tuple[str, str]] | None = None, *, ip_address: str = "",
                 value_context: dict | None = None, asset_type: str | None = None,
                 branding=None):
    """Raise (if breaching and not already open) or resolve one rule/device/iface."""
    existing = await _active_alert(db, rule.id, device_id, if_index)
    if existing:
        await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                              "|| CAST(:meta AS jsonb) WHERE id = :id"),
                         {'id': existing[0], 'meta': json.dumps(extra)})
    reading = ap.reading_text(rule.metric, value, rule.operator,
                              float(rule.threshold) if rule.threshold is not None else None,
                              value_context)
    notify = dict(reading=reading, iface_label=iface_label, device_id=device_id,
                  if_index=if_index, ip_address=ip_address, value=value,
                  value_context=value_context)
    if asset_type is not None or branding is not None:
        notify.update(asset_type=asset_type, branding=branding)
    if breach:
        # An active snooze suppresses re-raising this exact condition; the
        # resolve branch below still runs so a snoozed condition that clears
        # closes out any open alert.
        dedupe = f"rule:{rule.id}" + (f":if:{if_index}" if if_index is not None else "")
        if silences and (device_id, dedupe) in silences:
            return raised, resolved
        if existing is None:
            msg = _message(rule, hostname, detail, iface_label, ip_address)
            alert_id = await _raise(db, rule, device_id, msg, value, if_index, extra)
            if alert_id is not None:
                raised += 1
                await db.commit()
                sent = await _notify(db, rule, hostname, alert_id=alert_id,
                                     first_seen=datetime.now(timezone.utc), **notify)
                await ns.stamp(db, alert_id, sent)
                await db.commit()
        elif await ns.is_pending(db, existing[0]) and rule.notify_channels and \
                await _trigger_allowed(db, rule, device_id=device_id, if_index=if_index) and \
                await ns.claim_pending(db, existing[0]):
            # The trigger was suppressed by quiet hours and the condition is
            # still breaching. Without this the breach is never announced at
            # all: the alert row already exists, so the branch above never
            # runs again, and the only mail ever sent is the all-clear. The
            # claim (false -> sending, only while active) is committed before
            # the send, so a second pass or worker cannot send it as well.
            await db.commit()
            sent = await _notify(db, rule, hostname, alert_id=existing[0],
                                 first_seen=existing[1], checked=True, **notify)
            await ns.stamp(db, existing[0], sent)
            await db.commit()
    else:
        if existing is not None:
            alert_id, started_at = existing
            # An all-clear for a page nobody received is noise about an event
            # they never heard of, so it follows the trigger's fate.
            notified = await ns.was_notified(db, alert_id)
            resolved += await _resolve(db, alert_id)
            await db.commit()
            # Metric rules used to close silently, so whoever got the 2am page
            # never learned it had cleared — let alone after how long.
            if notified and getattr(rule, "recovery_alert", True):
                await _notify(db, rule, hostname, is_recovery=True,
                              duration=ap.duration_between(started_at),
                              alert_id=alert_id, first_seen=started_at, **notify)
    return raised, resolved


async def run_network_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import NETWORK_ALERT_PASS, run_pass
    return await run_pass(NETWORK_ALERT_PASS, evaluate_network_rules, session_factory)


async def network_alert_evaluator_loop() -> None:
    await asyncio.sleep(25)  # let the app boot and first SNMP metrics land
    while True:
        try:
            await run_network_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("network alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
