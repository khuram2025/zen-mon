"""NetPath alert evaluator.

Consumes structural events (path change, unreachable/reachable) the poller
records in ``netpath_events`` and evaluates numeric path metrics
(netpath_rtt / netpath_loss / netpath_hop_count) against the latest snapshot of
each probe. Mirrors udt_alert_service: event-driven metrics dedupe on a cooldown
window; numeric metrics raise/resolve statefully against the active alert.

Real path-change alerting is the single loudest unmet demand in the SolarWinds
NetPath community — theirs has been broken for years — so it is first-class here.
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

logger = logging.getLogger("netpath.alerts")

EVAL_INTERVAL_S = 30

# event_type -> metric key
_EVENT_METRIC = {
    "path_change": "netpath_path_change",
    "unreachable": "netpath_unreachable",
}
_NUMERIC_METRICS = ("netpath_rtt", "netpath_loss", "netpath_hop_count")


async def _rules(db: AsyncSession) -> list:
    prio = await alert_funnel.rule_priority_column(db)
    rows = (await db.execute(text(f"""
        SELECT id, name, metric, operator, threshold, severity, enabled, target,
               notify_channels, cooldown, schedule_start, schedule_end, schedule_days, {prio}
        FROM alert_rules
        WHERE enabled AND metric LIKE 'netpath\\_%' ESCAPE '\\'
    """))).mappings().all()
    return [dict(r) for r in rows]


def _scope_ok(rule, probe_name: str) -> bool:
    target = (rule.get("target") or "").strip()
    return not target or target.lower() == (probe_name or "").lower()


def _breaches(op: str, value: float, threshold: float) -> bool:
    op = op or ">="
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    return value >= threshold


async def _recent_alert_exists(db: AsyncSession, rule_id, dedupe: str, cooldown_s: int) -> bool:
    row = (await db.execute(text(
        "SELECT 1 FROM alerts WHERE rule_id = :rid AND metadata->>'netpath_dedupe' = :dk "
        "AND triggered_at > NOW() - make_interval(secs => :cd) LIMIT 1"
    ), {"rid": str(rule_id), "dk": dedupe, "cd": max(cooldown_s or 0, 60)})).first()
    return row is not None


async def _active_alert(db: AsyncSession, rule_id, dedupe: str):
    return (await db.execute(text(
        "SELECT id FROM alerts WHERE rule_id = :rid AND metadata->>'netpath_dedupe' = :dk "
        "AND status IN ('active','acknowledged') LIMIT 1"
    ), {"rid": str(rule_id), "dk": dedupe})).first()


async def _raise(db: AsyncSession, rule, message: str, meta: dict):
    """Insert the alert; returns its id."""
    meta = {"metric": rule["metric"], **meta}
    # The incident's priority is fixed when it is raised (migrate-164).
    p_col, p_val, p_params = alert_funnel.priority_insert(
        await alert_funnel.priority_supported(db), rule.get("priority"), rule["severity"] or "warning")
    row = (await db.execute(text(
        f"INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata{p_col}) "
        f"VALUES (NULL, :rid, 'active', :sev, :msg, NOW(), CAST(:meta AS jsonb){p_val}) RETURNING id"
    ), {"rid": str(rule["id"]), "sev": rule["severity"] or "warning",
        "msg": message, "meta": json.dumps(meta), **p_params})).first()
    return row[0] if row else None


async def _resolve(db: AsyncSession, alert_id) -> None:
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW() WHERE id = :id"), {"id": alert_id})


async def _notify(db: AsyncSession, rule, probe: dict, message: str, *, is_recovery: bool = False,
                  headline: dict | None = None, details: list | None = None,
                  alert_id=None, outbox: list | None = None) -> None:
    """Announce a NetPath alert or its recovery.

    With an ``outbox`` the send is queued and goes out after the pass commits
    (:func:`_flush`): the shared dispatcher commits keyed sends, and the event
    batch is row-locked until its transaction ends.
    """
    # Quiet hours: the rule's own schedule in the appliance time zone. (This
    # used to call notifications_allowed(db), which raised and was swallowed,
    # so quiet hours never applied here.)
    try:
        from app.services.alert_schedule import get_configured_timezone, notifications_allowed
        r = dict(rule)
        if not is_recovery and not notifications_allowed(r.get("schedule_start"), r.get("schedule_end"),
                                                r.get("schedule_days"), await get_configured_timezone(db)):
            return
    except Exception:
        logger.warning("quiet-hours check failed; notifying anyway", exc_info=True)
    try:
        from app.services.host_alert_service import dispatch_to_channels
        sev = rule["severity"] or "warning"
        status = "RESOLVED" if is_recovery else "ALERT"
        name = probe.get("name") or ""
        ctx = {
            "rule_name": rule["name"],
            "severity": sev,
            "status": status,
            "resolved": is_recovery,
            "hostname": name,
            "ip_address": str(probe.get("target_ip") or probe.get("target_host") or ""),
            "message": f"ZenPlus {'RECOVERY' if is_recovery else sev.upper()} — {rule['name']}: {message}",
            "subject": f"[{sev.upper()}] {'RESOLVED' if is_recovery else 'ALERT'}: {rule['name']}",
            "body": message if message.endswith(".") else f"{message}.",
            "rule_name_display": rule["name"],
            "headline_metric": headline,
            "details": details or [("Alert rule", rule["name"]), ("Probe", name)],
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "rule_id": str(rule["id"]),
            "is_recovery": is_recovery,
            # Keyed on the alert row: one send per incident event, channel and
            # recipient, linked to it in the delivery log.
            "alert_id": str(alert_id) if alert_id else None,
            "delivery_event": "recovery" if is_recovery else "trigger",
            **alert_funnel.notification_fields(sev, rule.get("priority"), trigger=not is_recovery),
        }
        channels = rule["notify_channels"] or []
        if isinstance(channels, str):
            channels = json.loads(channels)
        if outbox is not None:
            outbox.append((channels, ctx))
            return
        await dispatch_to_channels(db, channels, ctx)
    except Exception:
        logger.exception("netpath alert notify failed")


async def _flush(db: AsyncSession, outbox: list) -> None:
    """Send the notifications a committed pass queued."""
    from app.services.host_alert_service import dispatch_to_channels
    while outbox:
        channels, ctx = outbox.pop(0)
        try:
            await dispatch_to_channels(db, channels, ctx)
        except Exception:
            logger.exception("netpath alert notify failed")
            await db.rollback()


async def _process_events(db: AsyncSession, rules, outbox: list | None = None) -> int:
    evrules = [r for r in rules if r["metric"] in _EVENT_METRIC.values()]
    unreach_rules = [r for r in rules if r["metric"] == "netpath_unreachable"]
    rows = (await db.execute(text("""
        SELECT e.id, e.probe_id, e.event_type, e.severity, e.details,
               p.name AS probe_name, host(p.target_ip)::text AS target_ip, p.target_host
        FROM netpath_events e JOIN netpath_probes p ON p.id = e.probe_id
        WHERE NOT e.alerted
        ORDER BY e.id LIMIT 500
        FOR UPDATE OF e SKIP LOCKED
    """))).mappings().all()
    n = 0
    for ev in rows:
        probe = {"name": ev["probe_name"], "target_ip": ev["target_ip"], "target_host": ev["target_host"]}
        etype = ev["event_type"]
        # a 'reachable' event resolves any active unreachable alerts for the probe
        if etype == "reachable":
            for rule in unreach_rules:
                if not _scope_ok(rule, ev["probe_name"]):
                    continue
                active = await _active_alert(db, rule["id"], f"unreachable:{ev['probe_id']}")
                if active:
                    await _resolve(db, active[0])
                    await _notify(db, rule, probe, f"Path to {ev['probe_name']} is reachable again",
                                  is_recovery=True, alert_id=active[0], outbox=outbox)
            await db.execute(text("UPDATE netpath_events SET alerted = TRUE WHERE id = :id"), {"id": ev["id"]})
            continue

        metric = _EVENT_METRIC.get(etype)
        if metric:
            det = ev["details"] if isinstance(ev["details"], dict) else json.loads(ev["details"] or "{}")
            for rule in evrules:
                if rule["metric"] != metric or not _scope_ok(rule, ev["probe_name"]):
                    continue
                dedupe = f"{etype}:{ev['probe_id']}"
                if await _recent_alert_exists(db, rule["id"], dedupe, rule.get("cooldown") or 300):
                    continue
                if metric == "netpath_path_change":
                    msg = (f"Network path to {ev['probe_name']} changed "
                           f"({det.get('hop_count', '?')} hops, {det.get('num_paths', '?')} route(s))")
                    details = [("Alert rule", rule["name"]), ("Probe", ev["probe_name"]),
                               ("Hops", str(det.get("hop_count", "?"))),
                               ("Routes", str(det.get("num_paths", "?")))]
                else:
                    msg = f"Target {ev['probe_name']} became unreachable"
                    details = [("Alert rule", rule["name"]), ("Probe", ev["probe_name"]),
                               ("Last hops", str(det.get("last_hop_count", "?")))]
                alert_id = await _raise(db, rule, msg, {"netpath_dedupe": dedupe,
                                                        "probe_id": str(ev["probe_id"]),
                                                        "probe_name": ev["probe_name"], **det})
                await _notify(db, rule, probe, msg, details=details, alert_id=alert_id, outbox=outbox)
                n += 1
        await db.execute(text("UPDATE netpath_events SET alerted = TRUE WHERE id = :id"), {"id": ev["id"]})
    return n


async def _process_numeric(db: AsyncSession, rules, outbox: list | None = None) -> None:
    numrules = [r for r in rules if r["metric"] in _NUMERIC_METRICS]
    if not numrules:
        return
    probes = (await db.execute(text("""
        SELECT p.id, p.name, host(p.target_ip)::text AS target_ip, p.target_host, s.rtt_ms,
               s.loss_pct, s.hop_count, s.reached
        FROM netpath_probes p
        LEFT JOIN LATERAL (
            SELECT rtt_ms, loss_pct, hop_count, reached
            FROM netpath_snapshots WHERE probe_id = p.id ORDER BY run_at DESC LIMIT 1
        ) s ON TRUE
        WHERE p.enabled
    """))).mappings().all()

    for rule in numrules:
        metric = rule["metric"]
        for pr in probes:
            if not _scope_ok(rule, pr["name"]):
                continue
            if metric == "netpath_rtt":
                if not pr["reached"] or pr["rtt_ms"] is None:
                    continue
                value, unit, label = float(pr["rtt_ms"]), "ms", "Latency"
            elif metric == "netpath_loss":
                if pr["loss_pct"] is None:
                    continue
                value, unit, label = float(pr["loss_pct"]), "%", "Packet loss"
            else:  # netpath_hop_count
                if pr["hop_count"] is None:
                    continue
                value, unit, label = float(pr["hop_count"]), "hops", "Hop count"

            dedupe = f"{metric}:{pr['id']}"
            active = await _active_alert(db, rule["id"], dedupe)
            breach = _breaches(rule["operator"], value, float(rule["threshold"]))
            probe = {"name": pr["name"], "target_ip": pr["target_ip"], "target_host": pr["target_host"]}
            if breach and not active:
                disp = f"{value:g}{unit}" if unit != "hops" else f"{value:g} hops"
                msg = f"{label} to {pr['name']} is {disp} (threshold {rule['operator']} {rule['threshold']:g}{unit if unit!='hops' else ''})"
                headline = {"label": label, "value": disp,
                            "secondary_label": "Threshold", "secondary_value": f"{rule['operator']} {rule['threshold']:g}{unit if unit!='hops' else ''}"}
                alert_id = await _raise(db, rule, msg, {"netpath_dedupe": dedupe,
                                                        "probe_id": str(pr["id"]),
                                                        "probe_name": pr["name"],
                                                        "value": round(value, 2)})
                await _notify(db, rule, probe, msg, headline=headline,
                              details=[("Alert rule", rule["name"]), ("Probe", pr["name"]),
                                       (label, disp)], alert_id=alert_id, outbox=outbox)
            elif not breach and active:
                await _resolve(db, active[0])
                await _notify(db, rule, probe, f"{label} to {pr['name']} recovered ({value:g}{unit if unit!='hops' else ' hops'})",
                              is_recovery=True, alert_id=active[0], outbox=outbox)


async def run_eval_once(db: AsyncSession) -> None:
    rules = await _rules(db)
    if not rules:
        return
    outbox: list = []
    await _process_events(db, rules, outbox)
    await _process_numeric(db, rules, outbox)
    await db.commit()
    await _flush(db, outbox)


async def netpath_alert_evaluator_loop() -> None:
    await asyncio.sleep(28)
    logger.info("NetPath alert evaluator started (interval %ss)", EVAL_INTERVAL_S)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_eval_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netpath alert eval failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
