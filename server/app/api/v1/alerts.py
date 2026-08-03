from uuid import UUID
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertResponse, AlertStats
from app.services import alert_service
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])


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
    )
    data = [_serialize_alert(alert) for alert in alerts]

    return {
        "data": data,
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


@router.get("/stats", response_model=AlertStats)
async def alert_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await alert_service.get_alert_stats(db)


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

    result = await db.execute(
        select(
            Alert.device_id,
            func.count(Alert.id).label("total"),
            func.sum(case((Alert.status == "active", 1), else_=0)).label("active"),
            func.sum(case((Alert.severity == "critical", 1), else_=0)).label("critical"),
            func.sum(case((Alert.severity == "warning", 1), else_=0)).label("warning"),
            func.sum(case((Alert.severity == "info", 1), else_=0)).label("info"),
            func.max(Alert.triggered_at).label("last_triggered_at"),
        )
        .where(Alert.device_id.is_not(None))
        .where(Alert.triggered_at >= start)
        .where(Alert.triggered_at <= end)
        .group_by(Alert.device_id)
    )

    devices = {}
    for row in result.all():
        devices[str(row.device_id)] = {
            "total": int(row.total or 0),
            "active": int(row.active or 0),
            "critical": int(row.critical or 0),
            "warning": int(row.warning or 0),
            "info": int(row.info or 0),
            "last_triggered_at": row.last_triggered_at.isoformat() if row.last_triggered_at else None,
        }
    return {"from": start.isoformat(), "to": end.isoformat(), "devices": devices}


@router.get("/silences")
async def list_silences(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Active (non-expired) silences for a server. Declared before /{alert_id}
    so the literal path isn't captured as an alert id."""
    rows = (await db.execute(text(
        "SELECT id, dedupe, until, reason, created_at FROM alert_silences "
        "WHERE server_id = :sid AND (until IS NULL OR until > NOW()) "
        "ORDER BY created_at DESC"
    ), {"sid": server_id})).all()
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
        .options(selectinload(Alert.device), selectinload(Alert.service_check), selectinload(Alert.rule))
        .where(Alert.id == alert_id)
    )
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    related_query = (
        select(Alert)
        .options(selectinload(Alert.device), selectinload(Alert.service_check))
        .where(Alert.id != alert.id)
        .order_by(Alert.triggered_at.desc())
        .limit(8)
    )
    if alert.device_id:
        related_query = related_query.where(Alert.device_id == alert.device_id)
    elif alert.service_check_id:
        related_query = related_query.where(Alert.service_check_id == alert.service_check_id)
    elif alert.rule_id:
        related_query = related_query.where(Alert.rule_id == alert.rule_id)
    else:
        related_query = related_query.where(Alert.severity == alert.severity)

    related = (await db.execute(related_query)).scalars().all()
    payload = _serialize_alert(alert)
    payload["rule"] = {
        "id": alert.rule.id,
        "name": alert.rule.name,
        "metric": alert.rule.metric,
        "operator": alert.rule.operator,
        "threshold": alert.rule.threshold,
        "duration": alert.rule.duration,
        "cooldown": alert.rule.cooldown,
        "enabled": alert.rule.enabled,
    } if getattr(alert, "rule", None) else None
    payload["entity"] = {
        "kind": "device" if alert.device_id else "service_check" if alert.service_check_id else "system",
        "id": alert.device_id or alert.service_check_id,
        "name": alert.device.hostname if alert.device else alert.service_check.name if getattr(alert, "service_check", None) else "System",
    }
    payload["related_alerts"] = [_serialize_alert(item) for item in related]
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
    """(server_id, dedupe) for a server-scoped alert, or None if not silenceable."""
    row = (await db.execute(text(
        "SELECT server_id, metadata->>'dedupe' AS dedupe FROM alerts WHERE id = :id"
    ), {"id": alert_id})).first()
    if not row or not row.server_id or not row.dedupe:
        return None
    return str(row.server_id), row.dedupe


@router.post("/{alert_id}/snooze")
async def snooze_alert(
    alert_id: UUID,
    body: SnoozeBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Suppress this alert condition for a while (minutes) or forever (no minutes).

    Resolves the current alert and records a silence so the evaluator won't
    re-raise the same condition on this server until the silence expires.
    """
    key = await _alert_silence_key(db, alert_id)
    if not key:
        raise HTTPException(400, "Only server alerts can be snoozed")
    server_id, dedupe = key
    until_expr = "NOW() + make_interval(mins => :m)" if body.minutes else "NULL"
    # Clear any prior silence for this condition, then insert the new one.
    await db.execute(text(
        "DELETE FROM alert_silences WHERE server_id = :sid AND dedupe = :d"
    ), {"sid": server_id, "d": dedupe})
    await db.execute(text(
        f"INSERT INTO alert_silences (server_id, dedupe, until, created_by) "
        f"VALUES (:sid, :d, {until_expr}, :uid)"
    ), {"sid": server_id, "d": dedupe, "uid": user.id, **({"m": body.minutes} if body.minutes else {})})
    # Resolve current open alerts for this condition so the active view clears.
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW() "
        "WHERE server_id = :sid AND metadata->>'dedupe' = :d AND status IN ('active','acknowledged')"
    ), {"sid": server_id, "d": dedupe})
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
        raise HTTPException(400, "Only server alerts can be unsnoozed")
    server_id, dedupe = key
    res = await db.execute(text(
        "DELETE FROM alert_silences WHERE server_id = :sid AND dedupe = :d"
    ), {"sid": server_id, "d": dedupe})
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
