import json
from uuid import UUID
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertResponse, AlertStats
from app.services import alert_service
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])


def _device_alert_dedupe(rule_id, if_index) -> str:
    """Condition identity for device-scoped silences.

    Mirrors how the evaluators identify an alert instance: rule + device
    (+ interface for per-interface network metrics)."""
    d = f"rule:{rule_id}"
    if if_index not in (None, ""):
        d += f":if:{if_index}"
    return d


async def _attach_channels_and_silences(db: AsyncSession, items: list[dict]) -> None:
    """Batch-resolve each alert's rule notify_channels into channel objects and
    flag alerts whose condition is currently silenced.

    Done here (not per-row) so the list endpoint stays one query per concern,
    and exposed through /alerts because /settings/channels is admin-only while
    triage is an operator activity."""
    rule_ids = sorted({str(a["rule_id"]) for a in items if a.get("rule_id")})
    channels_by_rule: dict[str, list[dict]] = {}
    if rule_ids:
        rows = (await db.execute(text("""
            SELECT r.id::text AS rule_id, c.id::text AS ch_id, c.name, c.type, c.enabled
            FROM alert_rules r
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(r.notify_channels) = 'array'
                     THEN r.notify_channels ELSE '[]'::jsonb END) AS ch(id)
            JOIN notification_channels c ON c.id::text = ch.id
            WHERE r.id = ANY(CAST(:ids AS uuid[]))
            ORDER BY c.name
        """), {"ids": rule_ids})).all()
        for r in rows:
            channels_by_rule.setdefault(r.rule_id, []).append(
                {"id": r.ch_id, "name": r.name, "type": r.type, "enabled": r.enabled})

    silence_rows = (await db.execute(text(
        "SELECT server_id::text AS sid, device_id::text AS did, dedupe, until "
        "FROM alert_silences WHERE until IS NULL OR until > NOW()"
    ))).all()
    server_silences = {(r.sid, r.dedupe): r.until for r in silence_rows if r.sid}
    device_silences = {(r.did, r.dedupe): r.until for r in silence_rows if r.did}

    for a in items:
        a["channels"] = channels_by_rule.get(str(a["rule_id"]), []) if a.get("rule_id") else []
        until = None
        snoozed = False
        meta = a.get("metadata") or {}
        if a.get("server_id") and meta.get("dedupe"):
            key = (str(a["server_id"]), meta["dedupe"])
            if key in server_silences:
                snoozed, until = True, server_silences[key]
        elif a.get("device_id") and a.get("rule_id"):
            key = (str(a["device_id"]), _device_alert_dedupe(a["rule_id"], meta.get("if_index")))
            if key in device_silences:
                snoozed, until = True, device_silences[key]
        a["snoozed"] = snoozed
        a["snoozed_until"] = until


def _serialize_alert(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "rule_id": alert.rule_id,
        "device_id": alert.device_id,
        "server_id": alert.server_id,
        "device_hostname": alert.device.hostname if alert.device else None,
        "device_ip": str(alert.device.ip_address) if alert.device else None,
        "service_check_id": alert.service_check_id,
        "service_check_name": alert.service_check.name if getattr(alert, "service_check", None) else None,
        "sensor_id": alert.sensor_id,
        "sensor_name": alert.sensor.name if getattr(alert, "sensor", None) else None,
        "status": alert.status,
        "severity": alert.severity,
        "message": alert.message,
        "triggered_at": alert.triggered_at,
        "acknowledged_at": alert.acknowledged_at,
        "resolved_at": alert.resolved_at,
        "metadata": alert.extra_data or {},
    }


@router.get("", response_model=dict)
async def list_alerts(
    status: str | None = None,
    severity: str | None = None,
    device_id: UUID | None = None,
    server_id: UUID | None = None,
    service_check_id: UUID | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    alerts, total = await alert_service.get_alerts(
        db, status, severity, device_id, service_check_id, from_time, to_time, search, skip, limit,
        server_id=server_id,
        visible_tags=await scoping.visible_tags(db, user),
    )
    data = [_serialize_alert(alert) for alert in alerts]
    await _attach_channels_and_silences(db, data)

    return {
        "data": data,
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


@router.get("/stats", response_model=AlertStats)
async def alert_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await alert_service.get_alert_stats(
        db, visible_tags=await scoping.visible_tags(db, user))


@router.get("/device-counts")
async def alert_device_counts(
    hours: int = Query(default=24, ge=1, le=2160),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    end = to_time or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = from_time or (end - timedelta(hours=hours))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    # "Active" is a *current* state, not a window one: an alert that opened
    # before the selected range and is still firing must be counted, and a
    # resolved alert must not inflate the badge. Window-scoped history counts
    # (total / per-severity) are kept for context.
    from sqlalchemy import and_, or_

    in_window = and_(Alert.triggered_at >= start, Alert.triggered_at <= end)
    scope = await scoping.visible_tags(db, user)
    query = (
        select(
            Alert.device_id,
            func.sum(case((in_window, 1), else_=0)).label("total"),
            func.sum(case((Alert.status == "active", 1), else_=0)).label("active"),
            func.sum(case((and_(Alert.status == "active", Alert.severity == "critical"), 1), else_=0)).label("active_critical"),
            func.sum(case((and_(in_window, Alert.severity == "critical"), 1), else_=0)).label("critical"),
            func.sum(case((and_(in_window, Alert.severity == "warning"), 1), else_=0)).label("warning"),
            func.sum(case((and_(in_window, Alert.severity == "info"), 1), else_=0)).label("info"),
            func.max(Alert.triggered_at).label("last_triggered_at"),
        )
        .where(Alert.device_id.is_not(None))
        .where(or_(in_window, Alert.status == "active"))
        .group_by(Alert.device_id)
    )
    if scope is not None:
        query = query.where(
            text("EXISTS (SELECT 1 FROM devices _vd WHERE _vd.id = alerts.device_id AND "
                 + scoping.jsonb_tags_visible("_vd.tags") + ")")
            .bindparams(**{scoping.SCOPE_PARAM: scope})
        )
    result = await db.execute(query)

    devices = {}
    for row in result.all():
        devices[str(row.device_id)] = {
            "total": int(row.total or 0),
            "active": int(row.active or 0),
            "active_critical": int(row.active_critical or 0),
            "critical": int(row.critical or 0),
            "warning": int(row.warning or 0),
            "info": int(row.info or 0),
            "last_triggered_at": row.last_triggered_at.isoformat() if row.last_triggered_at else None,
        }
    return {"from": start.isoformat(), "to": end.isoformat(), "devices": devices}


@router.get("/silences")
async def list_silences(
    server_id: UUID | None = None,
    device_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Active (non-expired) silences for a server or device. Declared before
    /{alert_id} so the literal path isn't captured as an alert id."""
    if server_id is None and device_id is None:
        raise HTTPException(400, "server_id or device_id required")
    scope = "server_id = :sid" if server_id else "device_id = :sid"
    rows = (await db.execute(text(
        f"SELECT id, dedupe, until, reason, created_at FROM alert_silences "
        f"WHERE {scope} AND (until IS NULL OR until > NOW()) "
        f"ORDER BY created_at DESC"
    ), {"sid": server_id or device_id})).all()
    return {"items": [
        {"id": str(r.id), "dedupe": r.dedupe, "until": r.until,
         "forever": r.until is None, "created_at": r.created_at}
        for r in rows
    ]}


@router.delete("/silences/{silence_id}")
async def delete_silence(
    silence_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(text("DELETE FROM alert_silences WHERE id = :id"), {"id": silence_id})
    await db.commit()
    return {"removed": res.rowcount or 0}


@router.get("/{alert_id}")
async def get_alert_detail(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Alert)
        .options(selectinload(Alert.device), selectinload(Alert.service_check), selectinload(Alert.sensor), selectinload(Alert.rule))
        .where(Alert.id == alert_id)
    )
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    if not await scoping.alert_in_scope(db, alert_id, await scoping.visible_tags(db, user)):
        # 404, not 403: out-of-scope ids must not be confirmable.
        raise HTTPException(status_code=404, detail="Alert not found")

    related_query = (
        select(Alert)
        .options(selectinload(Alert.device), selectinload(Alert.service_check), selectinload(Alert.sensor))
        .where(Alert.id != alert.id)
        .order_by(Alert.triggered_at.desc())
        .limit(8)
    )
    if alert.device_id:
        related_query = related_query.where(Alert.device_id == alert.device_id)
    elif alert.service_check_id:
        related_query = related_query.where(Alert.service_check_id == alert.service_check_id)
    elif alert.sensor_id:
        related_query = related_query.where(Alert.sensor_id == alert.sensor_id)
    elif alert.rule_id:
        related_query = related_query.where(Alert.rule_id == alert.rule_id)
    else:
        related_query = related_query.where(Alert.severity == alert.severity)

    related = (await db.execute(related_query)).scalars().all()
    payload = _serialize_alert(alert)
    # The ORM model predates target/min_duration/trigger_on; read the full rule
    # row directly so the page can edit the rule without a second round-trip.
    payload["rule"] = None
    if alert.rule_id:
        rrow = (await db.execute(text(
            "SELECT id, name, metric, operator, threshold, duration, cooldown, enabled, "
            "       severity, target, min_duration, notify_channels, trigger_on, "
            "       recovery_alert, conditions "
            "FROM alert_rules WHERE id = :id"
        ), {"id": alert.rule_id})).first()
        if rrow:
            payload["rule"] = {
                "id": str(rrow.id),
                "name": rrow.name,
                "metric": rrow.metric,
                "operator": rrow.operator,
                "threshold": float(rrow.threshold) if rrow.threshold is not None else None,
                "duration": rrow.duration,
                "cooldown": rrow.cooldown,
                "enabled": rrow.enabled,
                "severity": rrow.severity,
                "target": rrow.target,
                "min_duration": rrow.min_duration,
                "notify_channels": rrow.notify_channels or [],
                "trigger_on": rrow.trigger_on,
                "recovery_alert": rrow.recovery_alert,
                "conditions": rrow.conditions or None,
            }
    payload["entity"] = {
        "kind": "device" if alert.device_id else "service_check" if alert.service_check_id else "sensor" if alert.sensor_id else "system",
        "id": alert.device_id or alert.service_check_id or alert.sensor_id,
        "name": alert.device.hostname if alert.device else alert.service_check.name if getattr(alert, "service_check", None) else alert.sensor.name if getattr(alert, "sensor", None) else "System",
    }
    payload["related_alerts"] = [_serialize_alert(item) for item in related]
    await _attach_channels_and_silences(db, [payload])
    return payload


@router.post("/{alert_id}/acknowledge", response_model=AlertResponse)
async def acknowledge_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    alert = await alert_service.acknowledge_alert(db, alert_id, user.id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    await write_audit_log(
        db,
        actor=user,
        action="alert.acknowledge",
        resource_type="alert",
        resource_id=str(alert.id),
        metadata={"severity": alert.severity, "device_id": str(alert.device_id) if alert.device_id else None},
    )
    await db.commit()
    return AlertResponse(
        id=alert.id,
        rule_id=alert.rule_id,
        device_id=alert.device_id,
        status=alert.status,
        severity=alert.severity,
        message=alert.message,
        triggered_at=alert.triggered_at,
        acknowledged_at=alert.acknowledged_at,
        resolved_at=alert.resolved_at,
    )


class SnoozeBody(BaseModel):
    minutes: Optional[int] = None  # None / 0 => mute forever


async def _alert_silence_key(db: AsyncSession, alert_id: UUID):
    """(scope_column, scope_id, dedupe) for a silenceable alert, or None.

    Server alerts carry an explicit metadata dedupe. Device alerts are
    identified by their rule (+ interface for per-interface metrics) — the
    same identity the evaluators use, so a silence maps 1:1 to the condition
    that raised the alert."""
    row = (await db.execute(text(
        "SELECT server_id, device_id, rule_id, "
        "       metadata->>'dedupe' AS dedupe, metadata->>'if_index' AS if_index "
        "FROM alerts WHERE id = :id"
    ), {"id": alert_id})).first()
    if not row:
        return None
    if row.server_id and row.dedupe:
        return "server_id", str(row.server_id), row.dedupe
    if row.device_id and row.rule_id:
        return "device_id", str(row.device_id), _device_alert_dedupe(row.rule_id, row.if_index)
    return None


@router.post("/{alert_id}/snooze")
async def snooze_alert(
    alert_id: UUID,
    body: SnoozeBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Suppress this alert condition for a while (minutes) or forever (no minutes).

    Resolves the current alert and records a silence so the evaluators won't
    re-raise the same condition on this server/device until the silence expires.
    """
    key = await _alert_silence_key(db, alert_id)
    if not key:
        raise HTTPException(400, "This alert has no rule identity to snooze")
    scope_col, scope_id, dedupe = key
    until_expr = "NOW() + make_interval(mins => :m)" if body.minutes else "NULL"
    # Clear any prior silence for this condition, then insert the new one.
    await db.execute(text(
        f"DELETE FROM alert_silences WHERE {scope_col} = :sid AND dedupe = :d"
    ), {"sid": scope_id, "d": dedupe})
    await db.execute(text(
        f"INSERT INTO alert_silences ({scope_col}, dedupe, until, created_by) "
        f"VALUES (:sid, :d, {until_expr}, :uid)"
    ), {"sid": scope_id, "d": dedupe, "uid": user.id, **({"m": body.minutes} if body.minutes else {})})
    # Resolve current open alerts for this condition so the active view clears.
    if scope_col == "server_id":
        await db.execute(text(
            "UPDATE alerts SET status = 'resolved', resolved_at = NOW() "
            "WHERE server_id = :sid AND metadata->>'dedupe' = :d AND status IN ('active','acknowledged')"
        ), {"sid": scope_id, "d": dedupe})
    else:
        row = (await db.execute(text(
            "SELECT rule_id, metadata->>'if_index' AS if_index FROM alerts WHERE id = :id"
        ), {"id": alert_id})).first()
        await db.execute(text(
            "UPDATE alerts SET status = 'resolved', resolved_at = NOW(), "
            "  metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
            "WHERE device_id = :sid AND rule_id = :rid "
            "  AND COALESCE(metadata->>'if_index','') = :ifx "
            "  AND status IN ('active','acknowledged')"
        ), {"sid": scope_id, "rid": str(row.rule_id), "ifx": row.if_index or "",
            "m": json.dumps({"resolved_by": "snooze", "snoozed_by": str(user.id)})})
    await write_audit_log(
        db, actor=user, action="alert.snooze", resource_type="alert",
        resource_id=str(alert_id),
        metadata={"minutes": body.minutes, "forever": not body.minutes, "dedupe": dedupe},
    )
    await db.commit()
    return {"snoozed": True, "forever": not body.minutes, "minutes": body.minutes}


@router.post("/{alert_id}/unsnooze")
async def unsnooze_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    key = await _alert_silence_key(db, alert_id)
    if not key:
        raise HTTPException(400, "This alert has no rule identity to unsnooze")
    scope_col, scope_id, dedupe = key
    res = await db.execute(text(
        f"DELETE FROM alert_silences WHERE {scope_col} = :sid AND dedupe = :d"
    ), {"sid": scope_id, "d": dedupe})
    await db.commit()
    return {"removed": res.rowcount or 0}


@router.post("/{alert_id}/resolve", response_model=AlertResponse)
async def resolve_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    alert = await alert_service.resolve_alert(db, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    await write_audit_log(
        db,
        actor=user,
        action="alert.resolve",
        resource_type="alert",
        resource_id=str(alert.id),
        metadata={"severity": alert.severity, "device_id": str(alert.device_id) if alert.device_id else None},
    )
    await db.commit()
    return AlertResponse(
        id=alert.id,
        rule_id=alert.rule_id,
        device_id=alert.device_id,
        status=alert.status,
        severity=alert.severity,
        message=alert.message,
        triggered_at=alert.triggered_at,
        acknowledged_at=alert.acknowledged_at,
        resolved_at=alert.resolved_at,
    )
