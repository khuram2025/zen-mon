"""SLO CRUD + error-budget status (AM-E6 / F7).

``apm_slos`` scope by ``service_id`` (FK to the apm_services registry). The
create path accepts a service *name* + env — what the UI has from the RED
list — and resolves/creates the registry row, so an SLO can be defined the
moment a service first reports, without waiting for the registry loop.

Burn evaluation lives in services/apm_slo_service.py (the loop); the budget
endpoint calls the same math so the chart and the alerts can never disagree.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.apm_slo_service import compute_slo_status

router = APIRouter(prefix="/apm/slos", tags=["APM SLOs"])

_SLI = "^(availability|latency|error_rate)$"  # 'custom' is schema-valid but not evaluatable yet

_SELECT = """
    SELECT s.id, s.name, s.operation, s.sli_type, s.latency_threshold_ms,
           s.target, s.window_days, s.burn_alert_enabled, s.notify_channels,
           s.created_at, s.updated_at,
           svc.id AS service_id, svc.name AS service_name, e.name AS env
    FROM apm_slos s
    JOIN apm_services svc ON svc.id = s.service_id
    LEFT JOIN apm_environments e ON e.id = svc.env_id
"""


class SloCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    service_name: str = Field(..., min_length=1, max_length=255)
    env: str = Field("prod", min_length=1, max_length=64)
    operation: Optional[str] = Field(None, max_length=255)
    sli_type: str = Field(..., pattern=_SLI)
    latency_threshold_ms: Optional[int] = Field(None, gt=0)
    target: float = Field(..., gt=0, lt=100)
    window_days: int = Field(30)
    burn_alert_enabled: bool = True
    notify_channels: list[str] = Field(default_factory=list)


class SloUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    operation: Optional[str] = Field(None, max_length=255)
    sli_type: Optional[str] = Field(None, pattern=_SLI)
    latency_threshold_ms: Optional[int] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0, lt=100)
    window_days: Optional[int] = None
    burn_alert_enabled: Optional[bool] = None
    notify_channels: Optional[list[str]] = None


def _row(r) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "service_id": str(r.service_id),
        "service_name": r.service_name,
        "env": r.env,
        "operation": r.operation,
        "sli_type": r.sli_type,
        "latency_threshold_ms": r.latency_threshold_ms,
        "target": float(r.target),
        "window_days": r.window_days,
        "burn_alert_enabled": r.burn_alert_enabled,
        "notify_channels": r.notify_channels or [],
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _validate(sli_type: str, latency_threshold_ms: Optional[int], window_days: int) -> None:
    if window_days not in (7, 30, 90):
        raise HTTPException(400, "window_days must be 7, 30 or 90")
    if sli_type == "latency" and not latency_threshold_ms:
        raise HTTPException(400, "latency_threshold_ms is required for latency SLIs")


async def _resolve_service(db: AsyncSession, name: str, env: str) -> UUID:
    """Find-or-create the apm_services registry row for (name, env)."""
    env_row = (await db.execute(text(
        "INSERT INTO apm_environments (name) VALUES (:n) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
    ), {"n": env})).first()
    row = (await db.execute(text(
        "INSERT INTO apm_services (name, env_id) VALUES (:n, :e) "
        "ON CONFLICT (name, env_id) DO UPDATE SET updated_at = NOW() RETURNING id"
    ), {"n": name, "e": env_row[0]})).first()
    return row[0]


@router.get("")
async def list_slos(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(text(_SELECT + " ORDER BY svc.name, s.name"))).all()
    return {"items": [_row(r) for r in rows]}


@router.post("", status_code=201)
async def create_slo(
    data: SloCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    _validate(data.sli_type, data.latency_threshold_ms, data.window_days)
    service_id = await _resolve_service(db, data.service_name.strip(), data.env.strip())
    row = (await db.execute(text(
        "INSERT INTO apm_slos (name, service_id, operation, sli_type, latency_threshold_ms, "
        " target, window_days, burn_alert_enabled, notify_channels) "
        "VALUES (:name, :sid, :op, :sli, :lat, :target, :wd, :burn, CAST(:nc AS jsonb)) "
        "RETURNING id"
    ), {
        "name": data.name, "sid": service_id, "op": data.operation,
        "sli": data.sli_type, "lat": data.latency_threshold_ms,
        "target": data.target, "wd": data.window_days,
        "burn": data.burn_alert_enabled,
        "nc": json.dumps([str(c) for c in data.notify_channels]),
    })).first()
    await db.commit()
    full = (await db.execute(text(_SELECT + " WHERE s.id = :id"), {"id": row[0]})).first()
    return _row(full)


@router.put("/{slo_id}")
async def update_slo(
    slo_id: UUID,
    data: SloUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")

    current = (await db.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slo_id})).first()
    if not current:
        raise HTTPException(404, "SLO not found")
    _validate(fields.get("sli_type", current.sli_type),
              fields.get("latency_threshold_ms", current.latency_threshold_ms),
              fields.get("window_days", current.window_days))

    sets, params = [], {"id": slo_id}
    for k, v in fields.items():
        if k == "notify_channels":
            sets.append("notify_channels = CAST(:nc AS jsonb)")
            params["nc"] = json.dumps([str(c) for c in v])
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = NOW()")
    await db.execute(text(f"UPDATE apm_slos SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    full = (await db.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slo_id})).first()
    return _row(full)


@router.delete("/{slo_id}")
async def delete_slo(
    slo_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(text("DELETE FROM apm_slos WHERE id = :id"), {"id": slo_id})
    await db.commit()
    if not res.rowcount:
        raise HTTPException(404, "SLO not found")
    return {"deleted": True}


@router.get("/{slo_id}/budget")
async def slo_budget(
    slo_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slo_id})).first()
    if not row:
        raise HTTPException(404, "SLO not found")
    slo = _row(row)
    status = await asyncio.to_thread(compute_slo_status, slo)
    return {"slo": slo, **status}
