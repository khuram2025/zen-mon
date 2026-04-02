from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload

from app.models.alert import Alert, AlertRule
from app.models.device import Device
from app.schemas.alert import AlertStats


async def get_alerts(
    db: AsyncSession,
    status: str | None = None,
    severity: str | None = None,
    device_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Alert], int]:
    query = select(Alert).options(selectinload(Alert.device))

    if status:
        query = query.where(Alert.status == status)
    if severity:
        query = query.where(Alert.severity == severity)
    if device_id:
        query = query.where(Alert.device_id == device_id)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    query = query.order_by(Alert.triggered_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    alerts = result.scalars().all()

    return alerts, total


async def acknowledge_alert(db: AsyncSession, alert_id: UUID, user_id: UUID) -> Alert | None:
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        return None

    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by = user_id
    await db.commit()
    await db.refresh(alert)
    return alert


async def resolve_alert(db: AsyncSession, alert_id: UUID) -> Alert | None:
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        return None

    alert.status = "resolved"
    alert.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(alert)
    return alert


async def get_alert_stats(db: AsyncSession) -> AlertStats:
    # Status counts
    result = await db.execute(
        select(Alert.status, func.count(Alert.id)).group_by(Alert.status)
    )
    status_counts = {row[0]: row[1] for row in result.all()}

    # Severity counts (active only)
    result = await db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.status == "active")
        .group_by(Alert.severity)
    )
    severity_counts = {row[0]: row[1] for row in result.all()}

    return AlertStats(
        active=status_counts.get("active", 0),
        acknowledged=status_counts.get("acknowledged", 0),
        resolved_today=status_counts.get("resolved", 0),
        critical=severity_counts.get("critical", 0),
        warning=severity_counts.get("warning", 0),
        info=severity_counts.get("info", 0),
    )
