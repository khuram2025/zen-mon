"""Host-metric alert evaluation.

Device/service alerting is event-driven (the Go poller pushes status changes to
alert_engine). Host metrics live in ClickHouse and arrive continuously, so they
need a *periodic* evaluator instead: every ``EVAL_INTERVAL_S`` this loads the
enabled host alert rules (``alert_rules`` rows whose ``metric`` starts with
``host_``), evaluates each against the current value for every in-scope server,
and raises/resolves a server-scoped alert via the shared helpers in
server_health_service.

A rule with ``server_id IS NULL`` applies to every agent server; a rule with a
``server_id`` applies only to that one. Alerts dedupe on ``rule:<rule_id>`` per
server, so running in both uvicorn workers is harmless (matches the staleness
sweep's approach). Agent-offline is already handled by that sweep.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap
from app.services.server_health_service import create_server_alert, resolve_server_alerts
from app.services.filesystem_monitoring import pg_capacity_filter

logger = logging.getLogger("zenplus.host_alerts")

EVAL_INTERVAL_S = 60
DEFAULT_WINDOW_S = 120  # sustained-window when a rule has no min_duration

# metric → (clickhouse table, column, aggregate) for the threshold metrics.
_CH_METRICS = {
    "host_cpu_pct": ("host_cpu_metrics", "cpu_total_pct", "avg"),
    "host_memory_pct": ("host_memory_metrics", "used_pct", "avg"),
    "host_disk_util_pct": ("host_disk_io_metrics", "util_pct", "avg"),
}

_OPS = {
    "gt": lambda a, b: a > b, ">": lambda a, b: a > b,
    "gte": lambda a, b: a >= b, ">=": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "<": lambda a, b: a < b,
    "lte": lambda a, b: a <= b, "<=": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "==": lambda a, b: a == b,
    "neq": lambda a, b: a != b, "!=": lambda a, b: a != b,
}


def _cmp(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get(operator)
    return bool(fn and fn(value, threshold))


def _ch_fleet(metric: str, window_s: int) -> dict[str, float]:
    """{server_id: aggregated value} for a ClickHouse host metric over a window."""
    from app.core.database import get_clickhouse_client

    table, col, agg = _CH_METRICS[metric]
    since = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        client = get_clickhouse_client()
        rows = client.query(
            f"SELECT server_id, {agg}({col}) FROM zenplus.{table} "
            "WHERE timestamp >= %(s)s GROUP BY server_id",
            parameters={"s": since},
        ).result_rows
        return {str(r[0]): float(r[1] or 0) for r in rows}
    except Exception as exc:
        logger.warning("host alert: clickhouse fleet query (%s) failed: %s", metric, exc)
        return {}


async def _fs_worst(db: AsyncSession) -> dict[str, tuple[float, str]]:
    """{server_id: (worst used_pct, mount)} from monitorable filesystem inventory."""
    rows = (await db.execute(text(
        f"""SELECT DISTINCT ON (server_id) server_id, used_pct, mount
           FROM server_filesystem_inventory
           WHERE used_pct IS NOT NULL AND {pg_capacity_filter()}
           ORDER BY server_id, used_pct DESC"""
    ))).all()
    return {str(r[0]): (float(r[1] or 0), r[2]) for r in rows}


async def _servers(db: AsyncSession) -> dict[str, str]:
    """{server_id: hostname} for agent servers that can be alerted on."""
    rows = (await db.execute(text(
        "SELECT id, hostname FROM servers "
        "WHERE collection_mode = 'agent' AND status <> 'disabled'"
    ))).all()
    return {str(r[0]): (r[1] or "server") for r in rows}


async def _gateway_config(db: AsyncSession, ch, gw_type: str) -> dict | None:
    gw_id = ch.gateway_id or (ch.config or {}).get("gateway_id")
    if gw_id:
        row = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways WHERE id = :id"),
            {"id": gw_id})).first()
    else:
        row = (await db.execute(text(
            "SELECT config, enabled FROM notification_gateways "
            "WHERE type = :t AND is_default = true LIMIT 1"
        ), {"t": gw_type})).first()
    if not row:
        return None
    # `enabled` is the column the Gateways page writes; config.enabled is only
    # whatever was in the blob when it was last saved. Honouring the column is
    # what makes "disable this gateway" actually stop delivery.
    if not row.enabled:
        return None
    return dict(row.config)


async def dispatch_to_channels(db: AsyncSession, channel_ids: list, ctx: dict) -> int:
    """Send a host alert to the rule's notification channels. Best-effort."""
    from app.api.v1.alert_engine import _send_email, _send_sms, _dispatch_channel

    sent = 0
    for ch_id in channel_ids or []:
        try:
            ch = (await db.execute(text(
                "SELECT type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"
            ), {"id": ch_id})).first()
            if not ch or not ch.enabled:
                continue
            cfg = ch.config or {}
            if ch.type == "email":
                rcpt = cfg.get("recipients", "")
                gw = await _gateway_config(db, ch, "smtp")
                if rcpt and gw:
                    from app.services.email_render import build_alert_email_html
                    from app.api.v1.alert_engine import _clean_details
                    mail_ctx = {
                        "severity": ctx.get("severity"),
                        "status": ctx.get("status"),
                        "resolved": bool(ctx.get("resolved")) or (ctx.get("status") == "RESOLVED"),
                        "title": ctx.get("rule_name"),
                        "hostname": ctx.get("hostname"),
                        "ip_address": ctx.get("ip_address"),
                        # `body` is the prose written for mail; `message` is the
                        # SMS line. Reading them the wrong way round put a 140-
                        # character telegram in the callout of a full-width mail.
                        "message": ctx.get("body") or ctx.get("message"),
                        "details": _clean_details(ctx.get("details") or []),
                        "headline_metric": ctx.get("headline_metric") or {},
                        "action_url": ctx.get("action_url") or "",
                        "timestamp": ctx.get("triggered_at"),
                    }
                    await _send_email(gw, rcpt, ctx["subject"], mail_ctx["message"],
                                      html_body=build_alert_email_html(mail_ctx))
                    sent += 1
            elif ch.type == "sms":
                phones = cfg.get("phone_numbers", "")
                gw = await _gateway_config(db, ch, "sms")
                if phones and gw:
                    await _send_sms(gw, phones, ctx["message"])
                    sent += 1
            elif ch.type in ("webhook", "slack", "teams", "discord", "pagerduty"):
                if await _dispatch_channel(ch.type, cfg, ctx):
                    sent += 1
        except Exception as exc:
            logger.warning("host alert: notify channel %s failed: %s", ch_id, exc)
    return sent


async def _current_value(db: AsyncSession, rule, server_id: str, ch_fleets: dict,
                         fs_worst: dict) -> tuple[bool, float, str] | None:
    """Return (breach, value, detail) for a rule against one server, or None to skip."""
    metric = rule.metric
    op, thr = rule.operator, float(rule.threshold or 0)

    if metric in _CH_METRICS:
        vals = ch_fleets.get(metric, {})
        if server_id not in vals:
            return None  # no recent data for this server
        v = vals[server_id]
        return _cmp(v, op, thr), v, f"{v:.1f}%"

    if metric == "host_filesystem_pct":
        if server_id not in fs_worst:
            return None
        v, mount = fs_worst[server_id]
        return _cmp(v, op, thr), v, f"{mount} at {v:.1f}%"

    if metric == "host_service_down":
        if not rule.target:
            return None
        row = (await db.execute(text(
            """SELECT count(*), max(service_name) FROM server_service_inventory
               WHERE server_id = :sid
                 AND service_name = :target
                 AND lower(state) IN ('stopped','stop_pending','dead','failed')"""
        ), {"sid": server_id, "target": rule.target})).first()
        n = int(row[0] or 0)
        return n > 0, float(n), (f"{rule.target} stopped" if n else f"{rule.target} running")

    if metric == "host_process_down":
        if not rule.target:
            return None
        row = (await db.execute(text(
            """SELECT count(*) FROM server_process_inventory
               WHERE server_id = :sid AND name = :target
                 AND updated_at >= NOW() - INTERVAL '300 seconds'"""
        ), {"sid": server_id, "target": rule.target})).first()
        running = int(row[0] or 0) > 0
        return (not running), (0.0 if running else 1.0), (f"{rule.target} {'running' if running else 'not running'}")

    return None


def _message(rule, hostname: str, detail: str) -> str:
    return f"{rule.name}: {hostname} — {detail}"


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


async def _open_alert_started(db: AsyncSession, server_id: str, dedupe: str):
    """When the open alert with this dedupe key was raised, or None."""
    return (await db.execute(text(
        """SELECT MIN(triggered_at) FROM alerts
           WHERE server_id = :sid AND status IN ('active', 'acknowledged')
             AND metadata->>'dedupe' = :dedupe"""
    ), {"sid": server_id, "dedupe": dedupe})).scalar()


async def _notify_rule(db: AsyncSession, rule, hostname: str, *, reading: str = "",
                       detail: str = "", is_recovery: bool = False,
                       duration: str = "") -> None:
    """Send a host metric alert, or its all-clear, in the shared house style."""
    sev = rule.severity or "warning"
    v = ap.rule_phrasing(rule, hostname=hostname, is_recovery=is_recovery,
                         reading=reading, duration=duration)
    v.update({"rule_name": rule.name or "Alert", "hostname": hostname,
              "ip_address": "", "severity": sev.upper(),
              "status": "RESOLVED" if is_recovery else "ALERT"})
    await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": _render(ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if is_recovery
                           else ap.DEFAULT_EMAIL_SUBJECT, v),
        "body": _render(ap.DEFAULT_RECOVERY_EMAIL_BODY if is_recovery
                        else ap.DEFAULT_EMAIL_BODY, v),
        "message": _render(ap.DEFAULT_RECOVERY_SMS if is_recovery else ap.DEFAULT_SMS, v),
        "hostname": hostname, "ip_address": "",
        "status": "RESOLVED" if is_recovery else "ALERT",
        "severity": sev, "resolved": is_recovery,
        "headline_metric": {
            "label": ap.metric_noun(rule.metric), "value": reading,
            "secondary_label": "Threshold", "secondary_value": v.get("threshold_value"),
        } if reading else None,
        "details": [("Alert rule", rule.name),
                    ("Condition", v.get("condition_label")),
                    # `detail` carries what the evaluator measured — which
                    # filesystem, which service — and nothing else knows it.
                    ("Measured", detail),
                    ("Active for", duration if is_recovery else None)],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
    })


async def evaluate_host_rules(db: AsyncSession) -> dict[str, int]:
    rules = (await db.execute(text(
        "SELECT id, name, metric, operator, threshold, severity, min_duration, "
        "notify_channels, server_id, target, recovery_alert, conditions, condition_logic, "
        "schedule_start, schedule_end, schedule_days "
        "FROM alert_rules WHERE enabled = true AND metric LIKE 'host\\_%'"
    ))).all()
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    _tz = await get_configured_timezone(db)
    servers = await _servers(db)
    fs_worst = await _fs_worst(db)
    # Pre-fetch ClickHouse fleet values once per metric/window combination.
    windows = {max(r.min_duration or 0, DEFAULT_WINDOW_S) for r in rules}
    ch_cache: dict[int, dict] = {}
    for w in windows:
        ch_cache[w] = {m: _ch_fleet(m, w) for m in _CH_METRICS}

    raised = resolved = 0
    for rule in rules:
        window = max(rule.min_duration or 0, DEFAULT_WINDOW_S)
        ch_fleets = ch_cache[window]
        targets = [str(rule.server_id)] if rule.server_id else list(servers)
        for sid in targets:
            hostname = servers.get(sid, "server")
            res = await _current_value(db, rule, sid, ch_fleets, fs_worst)
            if res is None:
                continue
            breach, value, detail = res
            dedupe = f"rule:{rule.id}"
            reading = ap.format_value(rule.metric, value)
            if breach:
                created = await create_server_alert(
                    db, sid, severity=rule.severity or "warning",
                    message=_message(rule, hostname, detail),
                    source="host_metric", dedupe=dedupe,
                    metadata={"rule_id": str(rule.id), "metric": rule.metric,
                              "value": round(value, 2), "threshold": float(rule.threshold or 0)},
                )
                if created:
                    raised += 1
                    await db.commit()
                    # Quiet hours: alert is recorded above; only suppress the
                    # outbound notification when outside the rule's schedule.
                    if not notifications_allowed(
                        getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
                        getattr(rule, "schedule_days", None), _tz,
                    ):
                        continue
                    await _notify_rule(db, rule, hostname, reading=reading, detail=detail)
            else:
                # Read when it started before closing it, so the all-clear can
                # say how long the host sat over the threshold.
                started_at = await _open_alert_started(db, sid, dedupe)
                n = await resolve_server_alerts(db, sid, dedupe)
                resolved += n
                if n and getattr(rule, "recovery_alert", True):
                    await db.commit()
                    await _notify_rule(db, rule, hostname, reading=reading, detail=detail,
                                       is_recovery=True,
                                       duration=ap.duration_between(started_at))
        await db.commit()

    if raised or resolved:
        logger.info("host alert eval: %d rules, %d raised, %d resolved", len(rules), raised, resolved)
    return {"rules": len(rules), "raised": raised, "resolved": resolved}


async def host_alert_evaluator_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(20)  # let the app boot and first metrics land
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await evaluate_host_rules(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("host alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
