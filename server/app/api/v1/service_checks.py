from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.service_check import (
    ServiceCheckCreate,
    ServiceCheckUpdate,
    ServiceCheckResponse,
    ServiceCheckSummary,
    ServiceMetricResponse,
)
from app.services import service_check_service, service_metric_service

router = APIRouter(prefix="/service-checks", tags=["Service Checks"])


@router.get("")
async def list_service_checks(
    device_id: UUID | None = None,
    check_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.get_service_checks(
        db, device_id=device_id, check_type=check_type, status=status,
        search=search, skip=skip, limit=limit,
    )


@router.get("/summary", response_model=ServiceCheckSummary)
async def service_check_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.get_service_check_summary(db)


@router.post("", response_model=ServiceCheckResponse, status_code=201)
async def create_service_check(
    data: ServiceCheckCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.create_service_check(db, data, current_user.id)


@router.get("/{check_id}", response_model=ServiceCheckResponse)
async def get_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sc = await service_check_service.get_service_check(db, check_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.put("/{check_id}", response_model=ServiceCheckResponse)
async def update_service_check(
    check_id: UUID,
    data: ServiceCheckUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sc = await service_check_service.update_service_check(db, check_id, data)
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.delete("/{check_id}", status_code=204)
async def delete_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await service_check_service.delete_service_check(db, check_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Service check not found")


class BulkDeleteRequest(BaseModel):
    check_ids: list[UUID]


@router.post("/bulk-delete")
async def bulk_delete_service_checks(
    data: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await service_check_service.bulk_delete_service_checks(db, data.check_ids)
    return {"deleted": count}


@router.get("/export/json")
async def export_service_checks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    checks = await service_check_service.export_service_checks(db)
    return JSONResponse(content=checks)


@router.get("/{check_id}/metrics", response_model=ServiceMetricResponse)
async def get_service_check_metrics(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="auto"),
    current_user: User = Depends(get_current_user),
):
    return service_metric_service.get_service_metrics(
        check_id, from_time=from_time, to_time=to_time, granularity=granularity,
    )


@router.get("/{check_id}/status-history")
async def get_service_check_status_history(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
):
    return service_metric_service.get_service_status_history(
        check_id, from_time=from_time, to_time=to_time, limit=limit,
    )
