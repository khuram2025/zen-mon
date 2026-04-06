from uuid import UUID
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.device import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceSummary, DeviceGroupResponse,
    BulkImportRequest, BulkImportResult,
)
from app.schemas.metric import MetricResponse, StatusChangeEvent
from app.services import device_service, metric_service

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.get("", response_model=dict)
async def list_devices(
    status: str | None = None,
    group_id: UUID | None = None,
    device_type: str | None = None,
    location: str | None = None,
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices, total = await device_service.get_devices(
        db, status, group_id, device_type, location, search, skip, limit
    )
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




@router.get("/dashboard/uptime-stats")
async def dashboard_uptime_stats(
    hours: int = Query(default=24, ge=1, le=8760),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get uptime percentages per device over a time range from ClickHouse metrics."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    to_time = datetime.utcnow()
    from_time = to_time - timedelta(hours=hours)

    # Try rollup tables first, fall back to raw
    tables_to_try = []
    if hours <= 6:
        tables_to_try = ["ping_metrics"]
    elif hours <= 168:
        tables_to_try = ["ping_metrics_5m", "ping_metrics"]
    else:
        tables_to_try = ["ping_metrics_1h", "ping_metrics_5m", "ping_metrics"]

    uptime_map = {}
    for table in tables_to_try:
        query = f"""
            SELECT device_id,
                   countIf(is_up = 1) AS up_count,
                   count() AS total_count
            FROM zenplus.{table}
            WHERE timestamp >= %(from)s AND timestamp <= %(to)s
            GROUP BY device_id
        """
        try:
            result = client.query(query, parameters={"from": from_time, "to": to_time})
            if len(result.result_rows) > 0:
                for row in result.result_rows:
                    device_id = str(row[0])
                    up = row[1]
                    total = row[2]
                    uptime_map[device_id] = round((up / total * 100) if total > 0 else 0, 2)
                break
        except Exception:
            continue

    return {"hours": hours, "from": from_time.isoformat(), "to": to_time.isoformat(), "devices": uptime_map}


@router.get("/groups", response_model=list[DeviceGroupResponse])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    groups = await device_service.get_device_groups(db)
    return groups


@router.get("/locations")
async def list_locations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_distinct_locations(db)


@router.post("/bulk-delete")
async def bulk_delete_devices(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ids = data.get("device_ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No device IDs provided")
    deleted = await device_service.bulk_delete_devices(db, [UUID(i) for i in ids])
    return {"deleted": deleted}


@router.post("", response_model=DeviceResponse, status_code=201)
async def create_device(
    data: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.create_device(db, data, user.id)
    return _device_to_response(device)


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_devices(
    data: BulkImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.bulk_import_devices(db, data.devices, user.id)


@router.get("/export")
async def export_devices(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices = await device_service.export_devices(db)
    return JSONResponse(content={"devices": devices})


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
