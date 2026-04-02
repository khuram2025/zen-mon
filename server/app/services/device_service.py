from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, text, cast, String
from sqlalchemy.orm import selectinload

from app.models.device import Device, DeviceGroup
from app.schemas.device import DeviceCreate, DeviceUpdate, DeviceSummary, DeviceBulkImportItem, BulkImportResult


async def get_devices(
    db: AsyncSession,
    status: str | None = None,
    group_id: UUID | None = None,
    device_type: str | None = None,
    location: str | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Device], int]:
    query = select(Device).options(selectinload(Device.group))

    if status:
        query = query.where(Device.status == status)
    if group_id:
        query = query.where(Device.group_id == group_id)
    if device_type:
        query = query.where(Device.device_type == device_type)
    if location:
        query = query.where(Device.location.ilike(f"%{location}%"))
    if search:
        query = query.where(
            Device.hostname.ilike(f"%{search}%") | Device.ip_address.cast(str).ilike(f"%{search}%")
        )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    # Paginate
    query = query.order_by(Device.hostname).offset(skip).limit(limit)
    result = await db.execute(query)
    devices = result.scalars().all()

    return devices, total


async def get_distinct_locations(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Device.location)
        .where(Device.location.isnot(None))
        .where(Device.location != "")
        .distinct()
        .order_by(Device.location)
    )
    return [row[0] for row in result.all()]


async def bulk_delete_devices(db: AsyncSession, device_ids: list[UUID]) -> int:
    result = await db.execute(
        delete(Device).where(Device.id.in_(device_ids))
    )
    await db.commit()
    return result.rowcount


async def get_device(db: AsyncSession, device_id: UUID) -> Device | None:
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).where(Device.id == device_id)
    )
    return result.scalar_one_or_none()


async def create_device(db: AsyncSession, data: DeviceCreate, user_id: UUID | None = None) -> Device:
    device = Device(
        hostname=data.hostname,
        ip_address=data.ip_address,
        device_type=data.device_type,
        location=data.location,
        group_id=data.group_id,
        tags=data.tags,
        ping_enabled=data.ping_enabled,
        ping_interval=data.ping_interval,
        description=data.description,
        created_by=user_id,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


async def update_device(db: AsyncSession, device_id: UUID, data: DeviceUpdate) -> Device | None:
    device = await get_device(db, device_id)
    if not device:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(device, key, value)

    await db.commit()
    await db.refresh(device)
    return device


async def delete_device(db: AsyncSession, device_id: UUID) -> bool:
    result = await db.execute(delete(Device).where(Device.id == device_id))
    await db.commit()
    return result.rowcount > 0


async def get_device_summary(db: AsyncSession) -> DeviceSummary:
    result = await db.execute(
        select(Device.status, func.count(Device.id)).group_by(Device.status)
    )
    counts = {row[0]: row[1] for row in result.all()}

    return DeviceSummary(
        total=sum(counts.values()),
        up=counts.get("up", 0),
        down=counts.get("down", 0),
        degraded=counts.get("degraded", 0),
        unknown=counts.get("unknown", 0),
        maintenance=counts.get("maintenance", 0),
    )


async def bulk_import_devices(
    db: AsyncSession,
    items: list[DeviceBulkImportItem],
    user_id: UUID | None = None,
) -> BulkImportResult:
    created = 0
    skipped = 0
    errors = []

    # Pre-fetch group name -> id mapping
    group_result = await db.execute(select(DeviceGroup.id, DeviceGroup.name))
    group_map = {row[1].lower(): row[0] for row in group_result.all()}

    for i, item in enumerate(items):
        try:
            # Resolve group name to ID
            group_id = None
            if item.group_name:
                group_id = group_map.get(item.group_name.lower())

            # Check for duplicate IP using raw SQL to avoid INET type issues
            dup_result = await db.execute(
                text("SELECT id FROM devices WHERE host(ip_address) = :ip"),
                {"ip": item.ip_address},
            )
            if dup_result.scalar_one_or_none():
                skipped += 1
                errors.append(f"Row {i+1}: IP {item.ip_address} already exists (skipped)")
                continue

            device = Device(
                hostname=item.hostname,
                ip_address=item.ip_address,
                device_type=item.device_type,
                location=item.location,
                group_id=group_id,
                tags=item.tags,
                ping_enabled=item.ping_enabled,
                ping_interval=item.ping_interval,
                description=item.description,
                created_by=user_id,
            )
            db.add(device)
            await db.flush()
            created += 1
        except Exception as e:
            await db.rollback()
            skipped += 1
            errors.append(f"Row {i+1}: {str(e)}")

    await db.commit()

    return BulkImportResult(
        total=len(items),
        created=created,
        skipped=skipped,
        errors=errors,
    )


async def export_devices(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.hostname)
    )
    devices = result.scalars().all()

    return [
        {
            "hostname": d.hostname,
            "ip_address": str(d.ip_address),
            "device_type": d.device_type,
            "location": d.location or "",
            "group_name": d.group.name if d.group else "",
            "tags": d.tags or [],
            "ping_enabled": d.ping_enabled,
            "ping_interval": d.ping_interval,
            "status": d.status,
            "last_rtt_ms": d.last_rtt_ms,
            "description": d.description or "",
        }
        for d in devices
    ]


async def get_device_groups(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(
            DeviceGroup,
            func.count(Device.id).label("device_count"),
        )
        .outerjoin(Device, Device.group_id == DeviceGroup.id)
        .group_by(DeviceGroup.id)
        .order_by(DeviceGroup.name)
    )
    groups = []
    for row in result.all():
        group = row[0]
        groups.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "color": group.color,
            "device_count": row[1],
        })
    return groups
