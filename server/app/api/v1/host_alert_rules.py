"""CRUD for server-scoped host-metric alert rules.

These live in the same ``alert_rules`` table as device/service rules (so they
share the alerts table, notification channels and the evaluator), but expose a
focused surface: a host metric, a comparison, a threshold, an optional target
(service/process/mount name) and a scope of one server or all servers.
"""

from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.services import alert_rule_scope as rs
from app.services.audit_service import write_audit_log

require_rule_manager = require_permission("alerts.manage")
from app.models.user import User

router = APIRouter(prefix="/host-alert-rules", tags=["Host Alert Rules"])

_METRIC = "^(host_cpu_pct|host_memory_pct|host_filesystem_pct|host_disk_util_pct|host_service_down|host_process_down)$"
_OPERATOR = "^(gt|gte|lt|lte|eq|neq|>|<|>=|<=|==|!=)$"

_COLUMNS = (
    "id, name, description, enabled, metric, operator, threshold, "
    "severity, min_duration, cooldown, notify_channels, server_id, target, "
    "created_at, updated_at"
)


class HostRuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    enabled: bool = True
    metric: str = Field(..., pattern=_METRIC)
    operator: str = Field("gt", pattern=_OPERATOR)
    threshold: float = 0
    severity: str = Field("warning", pattern="^(info|warning|critical)$")
    min_duration: int = Field(0, ge=0, le=86400)
    cooldown: int = Field(300, ge=0, le=86400)
    notify_channels: list[str] = Field(default_factory=list)
    server_id: Optional[UUID] = None
    target: Optional[str] = None


class HostRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    metric: Optional[str] = Field(None, pattern=_METRIC)
    operator: Optional[str] = Field(None, pattern=_OPERATOR)
    threshold: Optional[float] = None
    severity: Optional[str] = Field(None, pattern="^(info|warning|critical)$")
    min_duration: Optional[int] = Field(None, ge=0, le=86400)
    cooldown: Optional[int] = Field(None, ge=0, le=86400)
    notify_channels: Optional[list[str]] = None
    target: Optional[str] = None


def _row(r) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "description": r.description,
        "enabled": r.enabled,
        "metric": r.metric,
        "operator": r.operator,
        "threshold": float(r.threshold) if r.threshold is not None else None,
        "severity": r.severity,
        "min_duration": r.min_duration,
        "cooldown": r.cooldown,
        "notify_channels": r.notify_channels or [],
        "server_id": str(r.server_id) if r.server_id else None,
        "target": r.target,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


@router.get("")
async def list_host_rules(
    server_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List host rules. With server_id: rules that apply to it (its own + global)."""
    if server_id:
        rows = (await db.execute(text(
            f"SELECT {_COLUMNS} FROM alert_rules "
            "WHERE metric LIKE 'host\\_%' AND (server_id = :sid OR server_id IS NULL) "
            "ORDER BY server_id NULLS FIRST, metric, threshold"
        ), {"sid": server_id})).all()
    else:
        rows = (await db.execute(text(
            f"SELECT {_COLUMNS} FROM alert_rules WHERE metric LIKE 'host\\_%' "
            "ORDER BY server_id NULLS FIRST, metric, threshold"
        ))).all()
    return {"items": [_row(r) for r in rows]}


@router.post("")
async def create_host_rule(
    data: HostRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    if data.metric in ("host_process_down", "host_service_down") and not data.target:
        raise HTTPException(400, f"target is required for {data.metric}")
    await rs.guard_rule_change(db, user, data.model_dump())
    row = (await db.execute(text(
        "INSERT INTO alert_rules "
        "(name, description, enabled, metric, operator, threshold, severity, "
        " min_duration, cooldown, notify_channels, server_id, target, created_by) "
        "VALUES (:name, :description, :enabled, :metric, :operator, :threshold, :severity, "
        " :min_duration, :cooldown, CAST(:notify_channels AS jsonb), :server_id, :target, :created_by) "
        f"RETURNING {_COLUMNS}"
    ), {
        "name": data.name, "description": data.description, "enabled": data.enabled,
        "metric": data.metric, "operator": data.operator, "threshold": data.threshold,
        "severity": data.severity, "min_duration": data.min_duration, "cooldown": data.cooldown,
        "notify_channels": json.dumps([str(c) for c in data.notify_channels]),
        "server_id": data.server_id, "target": data.target, "created_by": user.id,
    })).first()
    await write_audit_log(db, actor=user, action="alert_rule.create", resource_type="alert_rule",
                          resource_id=str(row.id),
                          metadata={"name": data.name, "metric": data.metric,
                                    "server_id": str(data.server_id) if data.server_id else None})
    await db.commit()
    return _row(row)


@router.put("/{rule_id}")
async def update_host_rule(
    rule_id: UUID,
    data: HostRuleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    before = await rs.rule_row(db, rule_id)
    if not before or not str(before.get("metric") or "").startswith("host_"):
        raise HTTPException(404, "host alert rule not found")
    await rs.guard_rule_change(db, user, before, {**before, **fields})
    sets, params = [], {"id": rule_id}
    for k, v in fields.items():
        if k == "notify_channels":
            sets.append("notify_channels = CAST(:notify_channels AS jsonb)")
            params["notify_channels"] = json.dumps([str(c) for c in v])
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = NOW()")
    row = (await db.execute(text(
        f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = :id AND metric LIKE 'host\\_%' "
        f"RETURNING {_COLUMNS}"
    ), params)).first()
    if not row:
        raise HTTPException(404, "host alert rule not found")
    await write_audit_log(db, actor=user, action="alert_rule.update", resource_type="alert_rule",
                          resource_id=str(rule_id),
                          metadata={"changes": rs.rule_changes(before, await rs.rule_row(db, rule_id))})
    await db.commit()
    return _row(row)


@router.post("/{rule_id}/toggle")
async def toggle_host_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    before = await rs.rule_row(db, rule_id)
    if before:
        await rs.guard_rule_change(db, user, before)
    row = (await db.execute(text(
        "UPDATE alert_rules SET enabled = NOT enabled, updated_at = NOW() "
        f"WHERE id = :id AND metric LIKE 'host\\_%' RETURNING {_COLUMNS}"
    ), {"id": rule_id})).first()
    if not row:
        raise HTTPException(404, "host alert rule not found")
    await write_audit_log(db, actor=user, action="alert_rule.toggle", resource_type="alert_rule",
                          resource_id=str(rule_id), metadata={"enabled": row.enabled})
    await db.commit()
    return _row(row)


@router.delete("/{rule_id}")
async def delete_host_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    before = await rs.rule_row(db, rule_id)
    if before:
        await rs.guard_rule_change(db, user, before)
    res = await db.execute(text(
        "DELETE FROM alert_rules WHERE id = :id AND metric LIKE 'host\\_%'"
    ), {"id": rule_id})
    if not res.rowcount:
        raise HTTPException(404, "host alert rule not found")
    await write_audit_log(db, actor=user, action="alert_rule.delete", resource_type="alert_rule",
                          resource_id=str(rule_id),
                          metadata={"name": (before or {}).get("name"),
                                    "rule": rs.rule_changes({}, before)})
    await db.commit()
    return {"deleted": True}
