"""Hold-time ("Condition must exist for") confirmation for status alerts.

The status-transition engine fires at the instant a device or service check
changes state — it runs once, on the event, so it cannot wait out a rule's
``min_duration`` by itself. Instead it parks the alert row as
``status='pending'`` with ``metadata.hold_until``, and this sweeper settles
each row once the hold expires:

* Object still in the state that raised it -> promote to 'active' and dispatch
  the trigger notification (quiet hours and flap cooldown applied *now*, the
  moment anything would actually be sent).
* Object moved on (recovered, changed state, entered maintenance) -> resolve
  silently. The blip never becomes an alert and never pages, which is the
  entire point of the hold.

The engine also parks a trigger while its device is flapping (``flap_held``:
the hold is at least the policy's flap stable time). Such a row is settled
exactly like any other hold — confirmed if the device still has that status
when the hold expires, cancelled silently otherwise — so a sustained outage on
a flapping device still pages within the stable time.

A confirmed row is stamped ``metadata.notified_at``: escalation deadlines run
from the moment the trigger could first have been announced, not from the
transition, or a long hold would escalate the instant it confirms.

A held trigger does not supersede the rule's earlier alert that was already
sent (the engine only cancels an older hold); confirming the hold does. Until
then that alert stays open, so a recovery during the hold still closes it with
its all-clear. Confirmation takes the device's evaluation lock first, like the
engine, so the two never interleave on one device.

A recovery event arriving mid-hold already resolves the pending row in the
engine itself; this sweeper is the confirm side plus a safety net for rows the
event stream missed (poller restarts). Confirmation compares the object's
CURRENT status to the transition that raised the row — a metric-conditioned
rule (rtt/packet_loss) is not re-sampled here; the state check is the proxy.

Runs from the escalation sweeper loop (30s), so a hold of N seconds fires
between N and N+30s after the transition. On a DB without migrate-103 no
'pending' rows can exist and every sweep is a cheap no-op.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_funnel
from app.services import alert_notify_state as ns
from app.services import alert_phrasing as ap
from app.services.alert_schedule import get_configured_timezone, notifications_allowed
from app.services.alert_workflow import suppressed_sql

logger = logging.getLogger("zenplus.alert_hold")

_DUE_SQL = """
    SELECT a.id, a.rule_id, a.device_id, a.service_check_id, a.triggered_at,
           a.metadata, a.severity AS alert_severity, a.message,
           r.enabled AS rule_enabled, r.name AS rule_name,
           r.severity AS rule_severity, r.cooldown, r.min_duration,
           r.notify_channels, r.metric, r.operator, r.threshold, r.trigger_on,
           r.conditions, r.condition_logic,
           r.email_subject, r.email_body, r.sms_template,
           r.schedule_start, r.schedule_end, r.schedule_days, {rule_priority} AS rule_priority,
           d.status AS device_status, d.hostname AS device_hostname,
           host(d.ip_address) AS device_ip, d.device_type AS device_type,
           d.location AS device_location, dg.name AS device_group,
           sc.status AS check_status, sc.name AS check_name,
           sc.check_type AS check_type, sc.target_host AS check_target
    FROM alerts a
    JOIN alert_rules r ON r.id = a.rule_id
    LEFT JOIN devices d ON d.id = a.device_id
    LEFT JOIN device_groups dg ON dg.id = d.group_id
    LEFT JOIN service_checks sc ON sc.id = a.service_check_id
    WHERE a.status = 'pending'
      AND (a.metadata->>'hold_until') IS NOT NULL
      AND (a.metadata->>'hold_until')::timestamptz <= now()
"""


async def _due_sql(db: AsyncSession) -> str:
    """_DUE_SQL with the rule priority column only once migrate-164 added it
    (alert_funnel.rule_priority_column): a code update that lands before its
    migration must not stop confirming held alerts."""
    col = await alert_funnel.rule_priority_column(db)
    return _DUE_SQL.format(rule_priority="r.priority" if col == "priority" else "NULL")


async def _settle(db: AsyncSession, alert_id, *, status: str, extra: dict) -> bool:
    """Move a pending row to its settled state. False if something else
    (a recovery event, the other uvicorn worker) already settled it."""
    res = await db.execute(
        text(f"""
            UPDATE alerts
            SET status = :status,
                {'resolved_at = now(),' if status == 'resolved' else ''}
                metadata = COALESCE(metadata, '{{}}'::jsonb) || CAST(:m AS jsonb)
            WHERE id = :id AND status = 'pending'
        """),
        {"id": alert_id, "status": status, "m": json.dumps(extra)},
    )
    return bool(res.rowcount)


async def _supersede_on_confirm(db: AsyncSession, row, now: datetime, new_status: str) -> int:
    """Resolve the rule's other open status alerts on this device, as the
    engine does when it inserts a trigger that is not held. Device rows
    only: the service path has no supersede."""
    res = await db.execute(
        text("""
            UPDATE alerts
            SET status = 'resolved', resolved_at = :now,
                metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb)
            WHERE status IN ('active', 'acknowledged')
              AND device_id = :device_id AND rule_id = :rule_id
              AND service_check_id IS NULL
              AND id <> :id
              AND COALESCE(metadata->>'is_recovery', 'false') <> 'true'
              AND metadata->>'new_status' IS NOT NULL
        """),
        {"now": now, "device_id": row.device_id, "rule_id": row.rule_id, "id": row.id,
         "m": json.dumps({"superseded_by_status": new_status,
                          "resolved_by": "status_transition"})},
    )
    return res.rowcount or 0


def _meta_number(meta: dict, key: str):
    value = meta.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _held_readings(meta: dict) -> tuple:
    """(rtt ms, loss %) the transition measured. Rows from before 1.25.18
    stored only the status condition's own reading (condition_value)."""
    rtt = _meta_number(meta, "rtt_ms")
    loss = _meta_number(meta, "loss_pct")
    metric = meta.get("condition_metric")
    if rtt is None and "rtt_ms" not in meta and metric == "rtt":
        rtt = _meta_number(meta, "condition_value")
    if loss is None and "loss_pct" not in meta and metric == "packet_loss":
        loss = _meta_number(meta, "condition_value")
    return rtt, loss


def _device_phrasing(row, meta: dict, status: str, branding, now: datetime) -> dict:
    """The device variables the immediate path (alert_engine) builds: the
    reading, the status cause, the asset type and the transition's time."""
    from app.api.v1.alert_engine import RTT_UNAVAILABLE
    from app.services import alert_condition

    rtt, loss = _held_readings(meta)
    metric = row.metric or "ping_status"
    value = None
    if metric in alert_condition.RULE_READING_METRICS:
        value = _meta_number(meta, "rule_value")
        if value is None:
            value = rtt if metric == "rtt" else loss
    cause = alert_condition.status_cause(metric, status, meta.get("condition"))
    return {
        "rtt": (ap.format_reading("rtt", rtt, None, None) if rtt is not None
                else RTT_UNAVAILABLE if "rtt_ms" in meta or status == "down" else ""),
        "packet_loss": ap.format_reading("packet_loss", loss, None, None) if loss is not None else "",
        "group": getattr(row, "device_group", None) or "",
        "location": getattr(row, "device_location", None) or "",
        "device_type": getattr(row, "device_type", None) or "",
        **ap.rule_phrasing(
            row, hostname=ap.host_label(row.device_hostname or "monitored object", row.device_ip),
            reading=RTT_UNAVAILABLE if metric == "rtt" and value is None else None,
            value=value, device_status=status,
            asset_type=ap.asset_type_label(getattr(row, "device_type", None)),
            # When it changed, not when the hold confirmed it.
            event_time=row.triggered_at, branding=branding, now=now,
            status_detail=meta.get("condition") if status == "degraded" else None,
            status_cause=cause or None),
    }


async def _dispatch_confirmed(db: AsyncSession, row, now: datetime) -> int:
    """Render the rule's trigger templates and send them to its channels."""
    from app.api.v1.alert_engine import _dashboard_url, _render
    from app.services.host_alert_service import dispatch_to_channels
    from app.services.regional_time import notification_branding

    meta = dict(row.metadata or {})
    status_txt = (meta.get("new_status") or "alert").upper()
    is_service = row.service_check_id is not None
    hostname = (row.check_name if is_service else row.device_hostname) or "monitored object"
    ip = (row.check_target if is_service else row.device_ip) or ""
    sev = row.rule_severity or row.alert_severity or "warning"
    held_for = ap.duration_between(row.triggered_at, now)
    branding = await notification_branding(db)

    variables = {
        "hostname": hostname,
        "ip_address": ip,
        "status": status_txt,
        "severity": sev.upper(),
        "rule_name": row.rule_name or "Alert",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration": held_for,
        "rtt": "", "packet_loss": "",
        # {group} and {site} not known here are filled by the dispatcher
        # (notification_context) from the asset.
        "location": "", "device_type": "",
        "check_name": row.check_name or "",
        "check_type": row.check_type or "",
        "target": row.check_target or hostname,
        "error": (meta.get("error") or ""),
        **ap.service_check_variables(status_txt, meta.get("error")),
        "status_intro": "An alert has been triggered:",
        **(ap.rule_phrasing(row, hostname=ap.host_label(hostname, ip))
           if is_service else _device_phrasing(row, meta, status_txt.lower(), branding, now)),
    }
    incident = ap.incident_context(alert_id=row.id, first_seen=row.triggered_at, now=now,
                                   lifecycle="new")
    variables.update(incident)
    from app.services import notification_context
    for key, value in (await notification_context.asset_place(db, {"alert_id": row.id})).items():
        if not variables.get(key):
            variables[key] = value
    templates = ap.effective_templates(row, "service" if is_service else "device")
    subject = _render(templates["email_subject"], variables)
    body = _render(templates["email_body"], variables)
    sms = _render(templates["sms_template"], variables)
    if meta.get("flap_held"):
        # The hold came from flapping, not from the rule's own hold time.
        notice = (
            f"Flapping: this device kept changing status, so the alert waited "
            f"for the status to settle, and it was still {status_txt} after "
            f"{held_for}."
        )
    else:
        notice = (
            f"Hold time: this rule waits {int(row.min_duration or 0)} seconds "
            f"before alerting, and the condition was still present after "
            f"{held_for}."
        )

    return await dispatch_to_channels(db, row.notify_channels or [], {
        "subject": subject,
        "body": body,
        "message": sms,
        # The chat/incident title: the default mail sentence.
        "event_sentence": _render(ap.default_templates("service" if is_service else "device")["email_body"],
                                  variables),
        "hostname": hostname,
        "ip_address": ip,
        "status": status_txt,
        "severity": sev,
        "resolved": False,
        "rule_name": row.rule_name,
        "notice": notice,
        "details": [
            ("Alert rule", row.rule_name),
            ("Condition", variables.get("condition_label")),
            ("Condition held for", held_for),
            ("Target", row.check_target if is_service else None),
            ("Round-trip time", None if is_service else variables.get("rtt")),
            ("Packet loss", None if is_service else variables.get("packet_loss")),
            ("Group", variables.get("group")),
            ("Location", variables.get("location")),
            ("Device type", variables.get("device_type")),
        ],
        "triggered_at": now.isoformat(),
        "action_url": await _dashboard_url(db, "/services" if is_service else "/alerts"),
        "rule_id": str(row.rule_id),
        "is_recovery": False,
        "alert_id": str(row.id),
        # A confirmed hold is the incident's first page: label it New and
        # claim it once (alert_delivery_keys) however many workers sweep.
        "delivery_event": "trigger",
        # Priority routes and may group a confirmed warning like any other
        # first page (alert_funnel: P3/P4 warning/info triggers only).
        **alert_funnel.notification_fields(sev, getattr(row, "rule_priority", None), trigger=True),
        **incident,
    })


async def sweep_pending_holds(db: AsyncSession) -> dict[str, int]:
    """Settle every pending alert whose hold has expired."""
    rows = (await db.execute(text(await _due_sql(db)))).fetchall()
    if not rows:
        return {"checked": 0, "confirmed": 0, "cancelled": 0, "suppressed": 0}

    from app.api.v1.alert_engine import _within_cooldown

    now = datetime.now(timezone.utc)
    tz = await get_configured_timezone(db)
    confirmed = cancelled = suppressed = 0

    for row in rows:
        try:
            meta = dict(row.metadata or {})
            expected = (meta.get("new_status") or "").lower()
            current = ((row.check_status if row.service_check_id is not None
                        else row.device_status) or "").lower()

            # Arriving at 'up' is only an alert for a rule that asks for it;
            # a pending "is UP" row is a leftover of an 'any' rule and would
            # otherwise be confirmed by the very health it reports.
            stray_up = expected == "up" and (row.trigger_on or "any") != "up"
            if not row.rule_enabled or not expected or current != expected or stray_up:
                # The condition did not survive the hold (or the rule was
                # switched off mid-hold): the blip never becomes an alert.
                if await _settle(db, row.id, status="resolved", extra={
                    "hold_cancelled": True,
                    "hold_checked_at": now.isoformat(),
                    "status_at_check": current,
                }):
                    cancelled += 1
                await db.commit()
                continue

            device_row = row.device_id is not None and row.service_check_id is None
            if device_row:
                # The engine's per-device lock, taken before any alert row of
                # the device (see the module docstring).
                await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:device_id))"),
                                 {"device_id": str(row.device_id)})

            # Condition held: the alert becomes real. Settle the row first so
            # a crashed dispatch never re-pages on the next sweep.
            if not await _settle(db, row.id, status="active", extra={
                "hold_confirmed_at": now.isoformat(),
                # Escalation anchor (see the module docstring).
                "notified_at": now.isoformat(),
            }):
                await db.commit()
                continue
            if device_row:
                # Whatever the dispatch gates below decide, as in the engine.
                await _supersede_on_confirm(db, row, now, expected)

            # Quiet hours and flap cooldown apply at this moment — the moment
            # of dispatch — exactly as they would on a rule with no hold.
            allowed = notifications_allowed(
                row.schedule_start, row.schedule_end, row.schedule_days, tz)
            # A condition snoozed during its hold is recorded but stays quiet.
            if allowed and (await db.execute(text(
                    "SELECT " + suppressed_sql("a") + " FROM alerts a WHERE a.id = :id"),
                    {"id": row.id})).scalar():
                allowed = False
            if allowed:
                rule_ref = SimpleNamespace(id=row.rule_id, cooldown=row.cooldown)
                if await _within_cooldown(
                        db, rule_ref,
                        device_id=str(row.device_id) if row.device_id and row.service_check_id is None else None,
                        service_check_id=str(row.service_check_id) if row.service_check_id else None,
                        exclude_alert_id=row.id):
                    allowed = False
            await ns.stamp(db, row.id, allowed)
            if allowed:
                await _dispatch_confirmed(db, row, now)
                confirmed += 1
            else:
                suppressed += 1
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("hold sweep failed for alert %s", row.id)

    return {"checked": len(rows), "confirmed": confirmed,
            "cancelled": cancelled, "suppressed": suppressed}
