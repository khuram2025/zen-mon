"""UDT alert evaluator.

Consumes udt_events (row-locked batches, multi-worker safe) and raises
alerts for matching alert_rules with udt_* metrics:

  udt_new_endpoint      <- new_endpoint events
  udt_rogue_endpoint    <- rogue_detected events
  udt_watch_endpoint    <- watch_seen events
  udt_endpoint_moved    <- endpoint_moved events
  udt_port_capacity_pct <- periodic threshold over port usage

Event-driven rules use `threshold` only as an on/off (any value); the
capacity rule is a numeric percentage threshold with raise/resolve.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.services import alert_funnel

logger = logging.getLogger("zenplus.udt_alerts")

EVAL_INTERVAL_S = 30
EVENT_BATCH = 500

EVENT_METRIC = {
    "new_endpoint": "udt_new_endpoint",
    "rogue_detected": "udt_rogue_endpoint",
    "watch_seen": "udt_watch_endpoint",
    "endpoint_moved": "udt_endpoint_moved",
}

_EVENT_LABEL = {
    "new_endpoint": "New endpoint",
    "rogue_detected": "Rogue endpoint",
    "watch_seen": "Watched endpoint seen",
    "endpoint_moved": "Endpoint moved",
}


async def _udt_rules(db: AsyncSession) -> list:
    prio = await alert_funnel.rule_priority_column(db)
    return (await db.execute(text(
        f"""SELECT id, name, metric, operator, threshold, severity, enabled,
                  device_id, group_id, device_type, location, notify_channels, cooldown,
                  schedule_start, schedule_end, schedule_days, {prio}
           FROM alert_rules
           WHERE enabled AND metric LIKE 'udt\\_%' ESCAPE '\\'"""
    ))).mappings().all()


def _rule_scope_ok(rule, device: dict | None) -> bool:
    """Device-scoped rule filters; a rule with no scope matches all."""
    if rule["device_id"] is not None:
        return device is not None and str(device["id"]) == str(rule["device_id"])
    if rule["group_id"] is not None:
        return device is not None and str(device.get("group_id")) == str(rule["group_id"])
    if rule["device_type"]:
        return device is not None and device.get("device_type") == rule["device_type"]
    if rule["location"]:
        return device is not None and (device.get("location") or "") == rule["location"]
    return True


async def _recent_alert_exists(db: AsyncSession, rule_id, dedupe_key: str, cooldown_s: int) -> bool:
    row = (await db.execute(text(
        "SELECT 1 FROM alerts WHERE rule_id = :rid AND metadata->>'udt_dedupe' = :dk "
        "AND triggered_at > NOW() - make_interval(secs => :cd) LIMIT 1"
    ), {"rid": str(rule_id), "dk": dedupe_key, "cd": max(cooldown_s or 0, 60)})).first()
    return row is not None


async def _notify(db: AsyncSession, rule, message: str, device: dict | None, *,
                  alert_id=None, outbox: list | None = None) -> None:
    """Announce one raised UDT alert.

    With an ``outbox`` the send is queued and goes out after the batch commits
    (:func:`_flush`): the shared dispatcher commits keyed sends, and the event
    batch is row-locked until its transaction ends.
    """
    # Quiet hours: the rule's own schedule in the appliance time zone. (This
    # used to call notifications_allowed(db), which raised and was swallowed,
    # so quiet hours never applied here.)
    try:
        from app.services.alert_schedule import get_configured_timezone, notifications_allowed
        r = dict(rule)
        if not notifications_allowed(r.get("schedule_start"), r.get("schedule_end"),
                                                r.get("schedule_days"), await get_configured_timezone(db)):
            return
    except Exception:
        logger.warning("quiet-hours check failed; notifying anyway", exc_info=True)
    try:
        from app.services.host_alert_service import dispatch_to_channels
        from app.services import alert_phrasing as ap
        sev = (rule["severity"] or "warning")
        hostname = (device or {}).get("hostname") or ""
        # UDT events already describe themselves ("Rogue endpoint: laptop-7
        # (aa:bb:..) on sw-01 port 12"), so the message is the body. What was
        # missing is a details table shaped like every other alert's — this used
        # to hand the renderer a bare string where it expected label/value pairs.
        ctx = {
            "rule_name": rule["name"],
            "severity": sev,
            "status": "ALERT",
            "hostname": hostname,
            "ip_address": str((device or {}).get("ip_address") or ""),
            "message": f"ZenPlus {sev.upper()} — {rule['name']}: {message}",
            "subject": f"[{sev.upper()}] ALERT: {rule['name']}",
            "body": message if message.endswith(".") else f"{message}.",
            "details": [
                ("Alert rule", rule["name"]),
                ("Condition", ap.condition_label(rule["metric"], rule.get("operator"),
                                                 rule.get("threshold"))),
                ("Switch", hostname),
            ],
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "rule_id": str(rule["id"]),
            # Keyed on the alert row: one page per incident, channel and
            # recipient, linked to it in the delivery log.
            "alert_id": str(alert_id) if alert_id else None,
            "delivery_event": "trigger",
            **alert_funnel.notification_fields(sev, rule.get("priority"), trigger=True),
        }
        channels = rule["notify_channels"] or []
        if isinstance(channels, str):
            channels = json.loads(channels)
        if outbox is not None:
            outbox.append((channels, ctx))
            return
        await dispatch_to_channels(db, channels, ctx)
    except Exception:
        logger.exception("UDT alert notify failed")


async def _flush(db: AsyncSession, outbox: list) -> None:
    """Send the notifications a committed batch queued."""
    from app.services.host_alert_service import dispatch_to_channels
    while outbox:
        channels, ctx = outbox.pop(0)
        try:
            await dispatch_to_channels(db, channels, ctx)
        except Exception:
            logger.exception("UDT alert notify failed")
            await db.rollback()


async def _raise_alert(db: AsyncSession, rule, device_id, message: str, meta: dict):
    """Insert the alert; returns its id."""
    meta = {"metric": rule["metric"], **meta}
    # The incident's priority is fixed when it is raised (migrate-164).
    p_col, p_val, p_params = alert_funnel.priority_insert(
        await alert_funnel.priority_supported(db), rule.get("priority"), rule["severity"] or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata{p_col}) "
        f"VALUES (:did, :rid, 'active', :sev, :msg, NOW(), CAST(:meta AS jsonb){p_val}) RETURNING id"
    ), {"did": str(device_id) if device_id else None, "rid": str(rule["id"]),
        "sev": rule["severity"] or "warning", "msg": message, "meta": json.dumps(meta),
        **p_params})).first()
    return row[0] if row else None


async def _process_events(db: AsyncSession, rules, outbox: list | None = None) -> int:
    events = (await db.execute(text(
        f"""SELECT id, event_type, endpoint_id, device_id, if_index, details
            FROM udt_events WHERE NOT alerted
            ORDER BY id LIMIT {EVENT_BATCH}
            FOR UPDATE SKIP LOCKED"""
    ))).mappings().all()
    if not events:
        return 0

    device_cache: dict[str, dict | None] = {}

    async def get_device(dev_id) -> dict | None:
        if dev_id is None:
            return None
        k = str(dev_id)
        if k not in device_cache:
            row = (await db.execute(text(
                "SELECT id, hostname, ip_address, device_type, location, group_id "
                "FROM devices WHERE id = :id"
            ), {"id": k})).mappings().first()
            device_cache[k] = dict(row) if row else None
        return device_cache[k]

    raised = 0
    for ev in events:
        metric = EVENT_METRIC.get(ev["event_type"])
        if metric:
            matching = [r for r in rules if r["metric"] == metric]
            if matching:
                ep = (await db.execute(text(
                    """SELECT e.mac::text AS mac, e.hostname, host(e.ip_address) AS ip,
                              e.vendor, e.ignored
                       FROM udt_endpoints e WHERE e.id = :id"""
                ), {"id": ev["endpoint_id"]})).mappings().first()
                device = await get_device(ev["device_id"])
                if ep and not ep["ignored"]:
                    label = ep["hostname"] or ep["ip"] or ep["mac"]
                    port = f" port {ev['if_index']}" if ev["if_index"] else ""
                    where = f" on {device['hostname']}{port}" if device else ""
                    message = f"{_EVENT_LABEL[ev['event_type']]}: {label} ({ep['mac']}){where}"
                    for rule in matching:
                        if not _rule_scope_ok(rule, device):
                            continue
                        dedupe = f"{ev['event_type']}:{ep['mac']}"
                        if await _recent_alert_exists(db, rule["id"], dedupe, rule["cooldown"] or 300):
                            continue
                        details = ev["details"] or {}
                        if isinstance(details, str):
                            details = json.loads(details)
                        alert_id = await _raise_alert(db, rule, ev["device_id"], message, {
                            "udt_dedupe": dedupe, "mac": ep["mac"],
                            "endpoint_id": str(ev["endpoint_id"]),
                            "event_type": ev["event_type"], **details,
                        })
                        await _notify(db, rule, message, device, alert_id=alert_id, outbox=outbox)
                        raised += 1
        await db.execute(text("UPDATE udt_events SET alerted = TRUE WHERE id = :id"), {"id": ev["id"]})
    return raised


async def _process_capacity(db: AsyncSession, rules, outbox: list | None = None) -> None:
    cap_rules = [r for r in rules if r["metric"] == "udt_port_capacity_pct"]
    if not cap_rules:
        return
    usage = (await db.execute(text(
        """SELECT d.id, d.hostname, d.ip_address, d.device_type, d.location, d.group_id,
                  COUNT(*) FILTER (WHERE di.if_type IS NULL OR di.if_type IN (6, 117)) AS total,
                  COUNT(*) FILTER (WHERE di.oper_status = 'up' AND (di.if_type IS NULL OR di.if_type IN (6, 117))) AS used
           FROM devices d
           JOIN device_interfaces di ON di.device_id = d.id
           WHERE EXISTS (SELECT 1 FROM udt_port_state p WHERE p.device_id = d.id)
           GROUP BY d.id"""
    ))).mappings().all()
    for rule in cap_rules:
        thr = float(rule["threshold"] or 90)
        for dev in usage:
            if not _rule_scope_ok(rule, dict(dev)):
                continue
            total = dev["total"] or 0
            pct = (dev["used"] / total * 100.0) if total else 0.0
            dedupe = f"capacity:{dev['id']}"
            active = (await db.execute(text(
                "SELECT id FROM alerts WHERE rule_id = :rid AND metadata->>'udt_dedupe' = :dk "
                "AND status IN ('active','acknowledged') LIMIT 1"
            ), {"rid": str(rule["id"]), "dk": dedupe})).first()
            breach = pct >= thr if (rule["operator"] or ">=") in (">", ">=") else pct <= thr
            if breach and not active:
                msg = (f"Port capacity {pct:.0f}% on {dev['hostname']} "
                       f"({dev['used']}/{total} ports up, threshold {thr:.0f}%)")
                alert_id = await _raise_alert(db, rule, dev["id"], msg,
                                              {"udt_dedupe": dedupe, "value": round(pct, 1)})
                await _notify(db, rule, msg, dict(dev), alert_id=alert_id, outbox=outbox)
            elif not breach and active:
                await db.execute(text(
                    "UPDATE alerts SET status = 'resolved', resolved_at = NOW() WHERE id = :id"
                ), {"id": active[0]})


async def udt_alert_evaluator_loop() -> None:
    await asyncio.sleep(30)
    logger.info("UDT alert evaluator started (interval %ss)", EVAL_INTERVAL_S)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rules = await _udt_rules(db)
                outbox: list = []
                raised = await _process_events(db, rules, outbox)
                await _process_capacity(db, rules, outbox)
                await db.commit()
                await _flush(db, outbox)
                if raised:
                    logger.info("UDT evaluator raised %d alerts", raised)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("UDT alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
