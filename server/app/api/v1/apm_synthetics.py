"""APM synthetic scenario monitors — CRUD, run-now, and result history.

Definitions live in ``apm_synthetic_monitors`` (Postgres); run history in
``zenplus.apm_synthetic_results`` (ClickHouse). Execution is in
services/apm_synthetic_service.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, get_ch_client
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.apm_synthetic_service import execute_and_record, MAX_STEPS

router = APIRouter(prefix="/apm/synthetics", tags=["APM synthetics"])

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ALLOWED_ASSERTS = {"status_code", "latency_ms", "body_contains", "json_path"}
_ALLOWED_EXTRACT = {"json", "header", "regex"}


class StepModel(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    method: str = "GET"
    url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] = {}
    body: Optional[str] = Field(None, max_length=65536)
    assertions: list[dict] = []
    extract: list[dict] = []

    @field_validator("method")
    @classmethod
    def _method(cls, v: str) -> str:
        v = v.upper()
        if v not in _ALLOWED_METHODS:
            raise ValueError(f"method must be one of {sorted(_ALLOWED_METHODS)}")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://") or "{{" in v):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("assertions")
    @classmethod
    def _asserts(cls, v: list[dict]) -> list[dict]:
        for a in v:
            if (a.get("type") or "status_code") not in _ALLOWED_ASSERTS:
                raise ValueError(f"assertion type must be one of {sorted(_ALLOWED_ASSERTS)}")
        return v

    @field_validator("extract")
    @classmethod
    def _extract(cls, v: list[dict]) -> list[dict]:
        for e in v:
            if (e.get("from") or "json") not in _ALLOWED_EXTRACT:
                raise ValueError(f"extract 'from' must be one of {sorted(_ALLOWED_EXTRACT)}")
            if not e.get("var"):
                raise ValueError("extract entries need a 'var' name")
        return v


class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    steps: list[StepModel] = Field(min_length=1, max_length=MAX_STEPS)
    variables: dict[str, str] = {}
    verify_tls: bool = True
    notify_channels: list[str] = []
    check_interval: int = Field(60, ge=15, le=86400)
    timeout: int = Field(30, ge=1, le=120)
    retry_count: int = Field(1, ge=0, le=5)
    enabled: bool = True
    tags: list[str] = []


class MonitorUpdate(MonitorCreate):
    pass


def _config_json(body: MonitorCreate) -> str:
    return json.dumps({
        "steps": [s.model_dump(exclude_none=True) for s in body.steps],
        "variables": body.variables,
        "verify_tls": body.verify_tls,
        "notify_channels": body.notify_channels,
    })


_MONITOR_SELECT = """
    SELECT id, name, monitor_type, target_url, config, check_interval, timeout,
           retry_count, tags, enabled, status, last_check_at, created_at, updated_at
    FROM apm_synthetic_monitors
"""


def _row_to_dict(r) -> dict:
    cfg = r["config"] or {}
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "steps": cfg.get("steps") or [],
        "variables": cfg.get("variables") or {},
        "verify_tls": cfg.get("verify_tls", True),
        "notify_channels": cfg.get("notify_channels") or [],
        "check_interval": r["check_interval"],
        "timeout": r["timeout"],
        "retry_count": r["retry_count"],
        "tags": r["tags"] or [],
        "enabled": r["enabled"],
        "status": r["status"],
        "last_check_at": r["last_check_at"].isoformat() if r["last_check_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _uptime_stats_sync(monitor_ids: list[str], hours: int) -> dict[str, dict]:
    if not monitor_ids:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)
    rows = get_ch_client().query(
        """
        SELECT monitor_id, count(), countIf(success = 1),
               avg(total_ms), max(timestamp)
        FROM zenplus.apm_synthetic_results
        WHERE monitor_id IN %(ids)s AND timestamp >= %(since)s
        GROUP BY monitor_id
        """,
        parameters={"ids": [uuid.UUID(m) for m in monitor_ids], "since": since},
    ).result_rows
    return {
        str(r[0]): {
            "runs": int(r[1]),
            "uptime_pct": round(int(r[2]) / int(r[1]) * 100, 2) if r[1] else None,
            "avg_ms": round(float(r[3] or 0), 1),
            "last_run_at": r[4].isoformat() if isinstance(r[4], datetime) else None,
        }
        for r in rows
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("")
async def list_monitors(
    hours: int = Query(24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(text(_MONITOR_SELECT + " ORDER BY name"))).mappings().all()
    monitors = [_row_to_dict(r) for r in rows]
    stats = await asyncio.to_thread(
        _uptime_stats_sync, [m["id"] for m in monitors], hours)
    for m in monitors:
        m.update(stats.get(m["id"]) or {"runs": 0, "uptime_pct": None,
                                        "avg_ms": None, "last_run_at": None})
    return {
        "monitors": monitors,
        "summary": {
            "total": len(monitors),
            "up": sum(1 for m in monitors if m["status"] == "up"),
            "down": sum(1 for m in monitors if m["status"] == "down"),
            "disabled": sum(1 for m in monitors if not m["enabled"]),
        },
    }


@router.post("", status_code=201)
async def create_monitor(
    body: MonitorCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    monitor_id = str(uuid.uuid4())
    await db.execute(text("""
        INSERT INTO apm_synthetic_monitors
            (id, name, monitor_type, target_url, config, check_interval,
             timeout, retry_count, tags, enabled)
        VALUES (:id, :name, 'api', :url, CAST(:cfg AS jsonb), :ival,
                :timeout, :retries, CAST(:tags AS jsonb), :enabled)
    """), {
        "id": monitor_id, "name": body.name,
        "url": body.steps[0].url if body.steps else None,
        "cfg": _config_json(body), "ival": body.check_interval,
        "timeout": body.timeout, "retries": body.retry_count,
        "tags": json.dumps(body.tags), "enabled": body.enabled,
    })
    await db.commit()
    return {"id": monitor_id, "message": f"Scenario '{body.name}' created"}


@router.get("/{monitor_id}")
async def get_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text(_MONITOR_SELECT + " WHERE id = :id"), {"id": str(monitor_id)}
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Monitor not found")
    out = _row_to_dict(row)
    out.update((await asyncio.to_thread(
        _uptime_stats_sync, [out["id"]], 24)).get(out["id"]) or {})
    return out


@router.put("/{monitor_id}")
async def update_monitor(
    monitor_id: uuid.UUID,
    body: MonitorUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(text("""
        UPDATE apm_synthetic_monitors
        SET name = :name, target_url = :url, config = CAST(:cfg AS jsonb),
            check_interval = :ival, timeout = :timeout, retry_count = :retries,
            tags = CAST(:tags AS jsonb), enabled = :enabled, updated_at = NOW()
        WHERE id = :id
    """), {
        "id": str(monitor_id), "name": body.name,
        "url": body.steps[0].url if body.steps else None,
        "cfg": _config_json(body), "ival": body.check_interval,
        "timeout": body.timeout, "retries": body.retry_count,
        "tags": json.dumps(body.tags), "enabled": body.enabled,
    })
    if not res.rowcount:
        raise HTTPException(404, "Monitor not found")
    await db.commit()
    return {"message": "Scenario updated"}


@router.delete("/{monitor_id}")
async def delete_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(text(
        "DELETE FROM apm_synthetic_monitors WHERE id = :id"), {"id": str(monitor_id)})
    if not res.rowcount:
        raise HTTPException(404, "Monitor not found")
    await db.commit()
    return {"message": "Scenario deleted"}


@router.post("/{monitor_id}/run")
async def run_monitor_now(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Execute the scenario immediately and return full step-level detail."""
    row = (await db.execute(text("""
        SELECT id, name, monitor_type, target_url, config, check_interval,
               timeout, retry_count, status
        FROM apm_synthetic_monitors WHERE id = :id
    """), {"id": str(monitor_id)})).mappings().first()
    if not row:
        raise HTTPException(404, "Monitor not found")
    result = await execute_and_record(db, dict(row))
    return result


@router.get("/{monitor_id}/results")
async def monitor_results(
    monitor_id: uuid.UUID,
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    exists = (await db.execute(text(
        "SELECT 1 FROM apm_synthetic_monitors WHERE id = :id"),
        {"id": str(monitor_id)})).first()
    if not exists:
        raise HTTPException(404, "Monitor not found")

    def _fetch() -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)
        rows = get_ch_client().query(
            """
            SELECT timestamp, status, success, total_ms, steps_total,
                   steps_passed, failed_step, error, steps_json
            FROM zenplus.apm_synthetic_results
            WHERE monitor_id = %(id)s AND timestamp >= %(since)s
            ORDER BY timestamp DESC
            LIMIT %(lim)s
            """,
            parameters={"id": monitor_id, "since": since, "lim": limit},
        ).result_rows
        out = []
        for i, r in enumerate(rows):
            item = {
                "timestamp": r[0].isoformat(),
                "status": r[1], "success": bool(r[2]),
                "total_ms": int(r[3]), "steps_total": int(r[4]),
                "steps_passed": int(r[5]), "failed_step": r[6], "error": r[7],
            }
            # Full step detail only for the most recent runs — keeps payloads sane.
            if i < 25:
                try:
                    item["steps"] = json.loads(r[8])
                except (json.JSONDecodeError, TypeError):
                    item["steps"] = []
            out.append(item)
        return out

    return {"results": await asyncio.to_thread(_fetch)}
