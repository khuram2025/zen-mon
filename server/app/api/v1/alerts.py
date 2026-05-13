from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import get_current_user
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
    user: User = Depends(get_current_user),
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


@router.post("/{alert_id}/resolve", response_model=AlertResponse)
async def resolve_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
