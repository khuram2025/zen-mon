from uuid import UUID
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.service_check import ServiceCheck
from app.schemas.service_check import ServiceCheckCreate, ServiceCheckUpdate, ServiceCheckResponse, ServiceCheckSummary


def _to_response(sc: ServiceCheck) -> ServiceCheckResponse:
    return ServiceCheckResponse(
        id=sc.id,
        device_id=sc.device_id,
        device_hostname=sc.device.hostname if sc.device else None,
        name=sc.name,
        check_type=sc.check_type,
        enabled=sc.enabled,
        target_host=sc.target_host,
        target_port=sc.target_port,
        target_url=sc.target_url,
        http_method=sc.http_method or "GET",
        http_expected_status=sc.http_expected_status or 200,
        http_content_match=sc.http_content_match,
        http_follow_redirects=sc.http_follow_redirects if sc.http_follow_redirects is not None else True,
        tls_warn_days=sc.tls_warn_days or 30,
        tls_critical_days=sc.tls_critical_days or 7,
        check_interval=sc.check_interval or 60,
        timeout=sc.timeout or 10,
        status=sc.status or "unknown",
        last_check_at=sc.last_check_at,
        last_response_ms=sc.last_response_ms,
        last_error=sc.last_error,
        tls_expiry_date=sc.tls_expiry_date,
        tls_days_remaining=sc.tls_days_remaining,
        tls_issuer=sc.tls_issuer,
        tls_subject=sc.tls_subject,
        description=sc.description,
        created_at=sc.created_at,
        updated_at=sc.updated_at,
    )


async def get_service_checks(
    db: AsyncSession,
    device_id: UUID | None = None,
    check_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
):
    query = select(ServiceCheck)

    if device_id:
        query = query.where(ServiceCheck.device_id == device_id)
    if check_type:
        query = query.where(ServiceCheck.check_type == check_type)
    if status:
        query = query.where(ServiceCheck.status == status)
    if search:
        query = query.where(
            ServiceCheck.name.ilike(f"%{search}%") |
            ServiceCheck.target_host.ilike(f"%{search}%") |
            ServiceCheck.target_url.ilike(f"%{search}%")
        )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch
    query = query.order_by(ServiceCheck.name).offset(skip).limit(limit)
    result = await db.execute(query)
    checks = result.scalars().all()

    return {
        "data": [_to_response(sc) for sc in checks],
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


async def get_service_check(db: AsyncSession, check_id: UUID):
    result = await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))
    sc = result.scalar_one_or_none()
    if not sc:
        return None
    return _to_response(sc)


async def create_service_check(db: AsyncSession, data: ServiceCheckCreate, user_id: UUID):
    sc = ServiceCheck(
        **data.model_dump(),
        created_by=user_id,
    )
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    return _to_response(sc)


async def update_service_check(db: AsyncSession, check_id: UUID, data: ServiceCheckUpdate):
    result = await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))
    sc = result.scalar_one_or_none()
    if not sc:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(sc, key, value)

    await db.commit()
    await db.refresh(sc)
    return _to_response(sc)


async def delete_service_check(db: AsyncSession, check_id: UUID) -> bool:
    result = await db.execute(delete(ServiceCheck).where(ServiceCheck.id == check_id))
    await db.commit()
    return result.rowcount > 0


async def get_service_check_summary(db: AsyncSession) -> ServiceCheckSummary:
    result = await db.execute(
        select(ServiceCheck.status, func.count(ServiceCheck.id))
        .group_by(ServiceCheck.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    total = sum(counts.values())
    return ServiceCheckSummary(
        total=total,
        up=counts.get("up", 0),
        down=counts.get("down", 0),
        warning=counts.get("warning", 0),
        degraded=counts.get("degraded", 0),
        unknown=counts.get("unknown", 0),
    )


async def get_device_service_checks(db: AsyncSession, device_id: UUID):
    result = await db.execute(
        select(ServiceCheck)
        .where(ServiceCheck.device_id == device_id)
        .order_by(ServiceCheck.name)
    )
    checks = result.scalars().all()
    return [_to_response(sc) for sc in checks]


async def bulk_delete_service_checks(db: AsyncSession, check_ids: list[UUID]) -> int:
    result = await db.execute(
        delete(ServiceCheck).where(ServiceCheck.id.in_(check_ids))
    )
    await db.commit()
    return result.rowcount


async def export_service_checks(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(ServiceCheck).order_by(ServiceCheck.name))
    checks = result.scalars().all()
    export = []
    for sc in checks:
        export.append({
            "name": sc.name,
            "check_type": sc.check_type,
            "enabled": sc.enabled,
            "target_host": sc.target_host,
            "target_port": sc.target_port,
            "target_url": sc.target_url,
            "http_method": sc.http_method,
            "http_expected_status": sc.http_expected_status,
            "http_content_match": sc.http_content_match,
            "http_follow_redirects": sc.http_follow_redirects,
            "tls_warn_days": sc.tls_warn_days,
            "tls_critical_days": sc.tls_critical_days,
            "check_interval": sc.check_interval,
            "timeout": sc.timeout,
            "status": sc.status,
            "description": sc.description,
        })
    return export
