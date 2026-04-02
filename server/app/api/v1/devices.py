from uuid import UUID
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.device import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceSummary, DeviceGroupResponse,
)
from app.schemas.metric import MetricResponse, StatusChangeEvent
from app.services import device_service, metric_service

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.get("", response_model=dict)
async def list_devices(
    status: str | None = None,
    group_id: UUID | None = None,
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices, total = await device_service.get_devices(db, status, group_id, search, skip, limit)
    return {
        "data": [_device_to_response(d) for d in devices],
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


@router.get("/summary", response_model=DeviceSummary)
async def device_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_device_summary(db)


@router.get("/groups", response_model=list[DeviceGroupResponse])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    groups = await device_service.get_device_groups(db)
    return groups


@router.post("", response_model=DeviceResponse, status_code=201)
async def create_device(
    data: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.create_device(db, data, user.id)
    return _device_to_response(device)


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: UUID,
    data: DeviceUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.update_device(db, device_id, data)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    deleted = await device_service.delete_device(db, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")


@router.get("/{device_id}/metrics", response_model=MetricResponse)
async def get_device_metrics(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="auto", pattern="^(raw|5m|1h|auto)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Verify device exists
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_device_metrics(device_id, from_time, to_time, granularity)


@router.get("/{device_id}/status-history", response_model=list[StatusChangeEvent])
async def get_status_history(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_status_history(device_id, from_time, to_time, limit)


def _device_to_response(device) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        hostname=device.hostname,
        ip_address=str(device.ip_address),
        device_type=device.device_type,
        location=device.location,
        group_id=device.group_id,
        group_name=device.group.name if device.group else None,
        tags=device.tags or [],
        ping_enabled=device.ping_enabled,
        ping_interval=device.ping_interval,
        status=device.status,
        last_seen=device.last_seen,
        last_rtt_ms=device.last_rtt_ms,
        description=device.description,
        created_at=device.created_at,
        updated_at=device.updated_at,
    )
