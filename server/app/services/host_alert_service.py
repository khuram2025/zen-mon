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
        row = (await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})).first()
    else:
        row = (await db.execute(text(
            "SELECT config FROM notification_gateways WHERE type = :t AND is_default = true LIMIT 1"
        ), {"t": gw_type})).first()
    return dict(row.config) if row else None


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
                    await _send_email(gw, rcpt, ctx["subject"], ctx["body"])
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
        target = rule.target
        row = (await db.execute(text(
            """SELECT count(*), max(service_name) FROM server_service_inventory
               WHERE server_id = :sid
                 AND lower(state) IN ('stopped','stop_pending','dead','failed')
                 AND ( (:target)::text IS NULL
                       AND lower(start_mode) IN ('auto','automatic')
                       OR service_name = :target )"""
        ), {"sid": server_id, "target": target})).first()
        n = int(row[0] or 0)
        return n > 0, float(n), (f"{row[1]} stopped" if n else "all watched services running")

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


async def evaluate_host_rules(db: AsyncSession) -> dict[str, int]:
    rules = (await db.execute(text(
        "SELECT id, name, metric, operator, threshold, severity, min_duration, "
        "notify_channels, server_id, target "
        "FROM alert_rules WHERE enabled = true AND metric LIKE 'host\\_%'"
    ))).all()
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

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
                    msg = _message(rule, hostname, detail)
                    await dispatch_to_channels(db, rule.notify_channels or [], {
                        "subject": f"[{(rule.severity or 'warning').upper()}] {rule.name} — {hostname}",
                        "body": msg, "message": msg,
                        "hostname": hostname, "ip_address": "",
                        "status": "ALERT", "severity": rule.severity or "warning",
                        "triggered_at": datetime.now(timezone.utc).isoformat(),
                        "rule_id": str(rule.id), "rule_name": rule.name,
                    })
            else:
                n = await resolve_server_alerts(db, sid, dedupe)
                resolved += n
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
