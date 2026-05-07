from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])


class AuditLogResponse(BaseModel):
    id: UUID
    actor_id: Optional[UUID] = None
    actor_username: Optional[str] = None
    actor_role: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    metadata: dict
    created_at: datetime


@router.get("")
async def list_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    actor_id: UUID | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    where = []
    params: dict = {"skip": skip, "limit": limit}

    if action:
        where.append("action = :action")
        params["action"] = action
    if resource_type:
        where.append("resource_type = :resource_type")
        params["resource_type"] = resource_type
    if actor_id:
        where.append("actor_id = :actor_id")
        params["actor_id"] = actor_id

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    count = (await db.execute(
        text(f"SELECT COUNT(*) FROM audit_logs {where_sql}"),
        params,
    )).scalar() or 0

    rows = (await db.execute(
        text(
            f"""
            SELECT id, actor_id, actor_username, actor_role, action,
                   resource_type, resource_id, metadata, created_at
            FROM audit_logs
            {where_sql}
            ORDER BY created_at DESC
            OFFSET :skip LIMIT :limit
            """
        ),
        params,
    )).mappings().all()

    return {
        "data": [AuditLogResponse(**dict(row)) for row in rows],
        "meta": {"total": count, "skip": skip, "limit": limit},
    }
