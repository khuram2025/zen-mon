from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.alert import AlertResponse, AlertStats
from app.services import alert_service

router = APIRouter(prefix="/alerts", tags=["Alerts"])


@router.get("", response_model=dict)
async def list_alerts(
    status: str | None = None,
    severity: str | None = None,
    device_id: UUID | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    alerts, total = await alert_service.get_alerts(db, status, severity, device_id, skip, limit)
    data = []
    for alert in alerts:
        resp = AlertResponse(
            id=alert.id,
            rule_id=alert.rule_id,
            device_id=alert.device_id,
            device_hostname=alert.device.hostname if alert.device else None,
            device_ip=str(alert.device.ip_address) if alert.device else None,
            status=alert.status,
            severity=alert.severity,
            message=alert.message,
            triggered_at=alert.triggered_at,
            acknowledged_at=alert.acknowledged_at,
            resolved_at=alert.resolved_at,
        )
        data.append(resp)

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


@router.post("/{alert_id}/acknowledge", response_model=AlertResponse)
async def acknowledge_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    alert = await alert_service.acknowledge_alert(db, alert_id, user.id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
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
