"""Settings -> System status: the appliance's own health.

``GET /api/v1/system/status``          current CPU, memory, services, data stores
``GET /api/v1/system/status/history``  the last hours of one-minute samples

Read-only and administrator-only, like the storage routes beside it on the
page. The public ``/api/v1/system/health`` stays the liveness check installers
and the updater call; this is the detailed view for a signed-in administrator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.security import require_admin_user
from app.models.user import User
from app.services import appliance_health

router = APIRouter(prefix="/system/status", tags=["System"])


@router.get("")
async def system_status(user: User = Depends(require_admin_user)):
    return await appliance_health.status()


@router.get("/history")
async def system_status_history(
    hours: int = Query(appliance_health.HISTORY_HOURS, ge=1, le=appliance_health.HISTORY_HOURS),
    user: User = Depends(require_admin_user),
):
    return await appliance_health.history(hours)
