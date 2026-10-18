"""CRUD for server-scoped host-metric alert rules.

These live in the same ``alert_rules`` table as device/service rules (so they
share the alerts table, notification channels and the evaluator), but expose a
focused surface: a host metric, a comparison, a threshold, an optional target
(service/process/mount name) and a scope of one server or all servers.

Incident lifecycle (host_alert_service): ``min_duration`` is how long the
breach must persist; ``clear_duration`` how long the cleared condition must
hold before the incident resolves (null = the 300 s default, 0 = at once);
``reset_threshold`` the level it must clear (null = automatic for percent
metrics: 2 points on the healthy side, returned as ``auto_reset_threshold``);
``recovery_alert`` whether a recovery notice is sent. The reset level is
stored as the rule's single condition, like network rules.
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
from app.services.alert_priority import priority_for
from app.services.audit_service import write_audit_log

require_rule_manager = require_permission("alerts.manage")
from app.models.user import User

router = APIRouter(prefix="/host-alert-rules", tags=["Host Alert Rules"])

_METRIC = "^(host_cpu_pct|host_memory_pct|host_filesystem_pct|host_disk_util_pct|host_service_down|host_process_down)$"
_OPERATOR = "^(gt|gte|lt|lte|eq|neq|>|<|>=|<=|==|!=)$"

_COLUMNS = (
    "id, name, description, enabled, metric, operator, threshold, "
    "severity, min_duration, clear_duration, cooldown, notify_channels, server_id, target, "
    "recovery_alert, conditions, priority, escalation_policy_id, created_at, updated_at"
)
# Threshold metrics: the ones a reset threshold and clear hold apply to.
_PCT_METRICS = ("host_cpu_pct", "host_memory_pct", "host_filesystem_pct", "host_disk_util_pct")


class HostRuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    enabled: bool = True
    metric: str = Field(..., pattern=_METRIC)
    operator: str = Field("gt", pattern=_OPERATOR)
    threshold: float = 0
    severity: str = Field("warning", pattern="^(info|warning|critical)$")
    min_duration: int = Field(0, ge=0, le=86400)
    # None = the evaluator default (300 s); 0 = resolve on the first healthy sample.
    clear_duration: Optional[int] = Field(None, ge=0, le=86400)
    # None = automatic for percent metrics (threshold -/+ 2 points).
    reset_threshold: Optional[float] = Field(None, allow_inf_nan=False)
    recovery_alert: bool = False
    cooldown: int = Field(300, ge=0, le=86400)
    notify_channels: list[str] = Field(default_factory=list)
    server_id: Optional[UUID] = None
    target: Optional[str] = None
    # None = derived from severity (critical -> P2, warning -> P3, info -> P4).
    priority: Optional[str] = Field(None, pattern="^P[1-4]$")
    # None = the priority's default escalation policy.
    escalation_policy_id: Optional[UUID] = None


class HostRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    metric: Optional[str] = Field(None, pattern=_METRIC)
    operator: Optional[str] = Field(None, pattern=_OPERATOR)
    threshold: Optional[float] = None
    severity: Optional[str] = Field(None, pattern="^(info|warning|critical)$")
    min_duration: Optional[int] = Field(None, ge=0, le=86400)
    clear_duration: Optional[int] = Field(None, ge=0, le=86400)
    reset_threshold: Optional[float] = Field(None, allow_inf_nan=False)
    recovery_alert: Optional[bool] = None
    cooldown: Optional[int] = Field(None, ge=0, le=86400)
    notify_channels: Optional[list[str]] = None
    target: Optional[str] = None
    priority: Optional[str] = Field(None, pattern="^P[1-4]$")
    escalation_policy_id: Optional[UUID] = None


async def _policy_or_422(db: AsyncSession, policy_id) -> Optional[UUID]:
    if policy_id is None:
        return None
    if not (await db.execute(text("SELECT 1 FROM escalation_policies WHERE id = :id"),
                             {"id": policy_id})).first():
        raise HTTPException(422, "Escalation policy not found")
    return policy_id


def _channels(ids) -> str:
    """notify_channels as stored: string ids, each once, in order."""
    return json.dumps(list(dict.fromkeys(str(c) for c in ids or [] if c not in (None, ""))))


def _conditions(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _reset_of(conditions, metric) -> Optional[float]:
    for c in _conditions(conditions):
        if isinstance(c, dict) and c.get("metric") == metric and c.get("reset_threshold") is not None:
            return float(c["reset_threshold"])
    return None


def _condition_json(metric, operator, threshold, reset) -> Optional[str]:
    """The stored condition for an explicit reset level (None = no condition
    row: the flat columns plus the automatic reset). 422 when the reset level
    is not on the healthy side of the trigger."""
    if reset is None:
        return None
    if metric not in _PCT_METRICS:
        raise HTTPException(422, "A reset threshold applies to CPU, memory, filesystem and disk utilisation rules")
    from app.services.network_conditions import validate_conditions
    cond = {"metric": metric, "operator": operator, "threshold": float(threshold or 0),
            "reset_threshold": float(reset)}
    try:
        validate_conditions([cond])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return json.dumps([cond])


def _auto_reset(metric, operator, threshold) -> Optional[float]:
    from app.services.network_conditions import auto_reset_threshold
    return auto_reset_threshold({"metric": metric, "operator": operator, "threshold": threshold})


def _row(r) -> dict:
    threshold = float(r.threshold) if r.threshold is not None else None
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
        "clear_duration": r.clear_duration,
        "reset_threshold": _reset_of(r.conditions, r.metric),
        "auto_reset_threshold": _auto_reset(r.metric, r.operator, threshold),
        "recovery_alert": bool(r.recovery_alert),
        "cooldown": r.cooldown,
        "notify_channels": r.notify_channels or [],
        "server_id": str(r.server_id) if r.server_id else None,
        "target": r.target,
        "priority": r.priority,
        "effective_priority": priority_for(r.priority, r.severity),
        "escalation_policy_id": str(r.escalation_policy_id) if r.escalation_policy_id else None,
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
    conditions = _condition_json(data.metric, data.operator, data.threshold, data.reset_threshold)
    row = (await db.execute(text(
        "INSERT INTO alert_rules "
        "(name, description, enabled, metric, operator, threshold, severity, "
        " min_duration, clear_duration, cooldown, notify_channels, server_id, target, "
        " recovery_alert, conditions, priority, escalation_policy_id, created_by) "
        "VALUES (:name, :description, :enabled, :metric, :operator, :threshold, :severity, "
        " :min_duration, :clear_duration, :cooldown, CAST(:notify_channels AS jsonb), :server_id, :target, "
        " :recovery_alert, CAST(:conditions AS jsonb), :priority, :escalation_policy_id, :created_by) "
        f"RETURNING {_COLUMNS}"
    ), {
        "name": data.name, "description": data.description, "enabled": data.enabled,
        "metric": data.metric, "operator": data.operator, "threshold": data.threshold,
        "severity": data.severity, "min_duration": data.min_duration,
        "clear_duration": data.clear_duration, "cooldown": data.cooldown,
        "notify_channels": _channels(data.notify_channels),
        "server_id": data.server_id, "target": data.target,
        "recovery_alert": data.recovery_alert, "conditions": conditions,
        "priority": data.priority,
        "escalation_policy_id": await _policy_or_422(db, data.escalation_policy_id),
        "created_by": user.id,
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
    # NOT NULL columns: an explicit null means "leave it as it is".
    for key in ("recovery_alert", "enabled", "metric", "operator", "threshold",
                "min_duration", "cooldown", "name", "severity"):
        if key in fields and fields[key] is None:
            del fields[key]
    # The reset level lives in the rule's condition; rebuild it whenever the
    # trigger or the reset changes (a reset left on the wrong side is a 422).
    if any(k in fields for k in ("metric", "operator", "threshold", "reset_threshold")):
        merged = {**before, **fields}
        if "reset_threshold" in fields:
            reset = fields.pop("reset_threshold")
        else:
            reset = _reset_of(before.get("conditions"), before.get("metric"))
            if merged.get("metric") not in _PCT_METRICS:
                reset = None   # a stored reset does not follow the rule to a non-threshold metric
        fields["conditions"] = _condition_json(merged.get("metric"), merged.get("operator"),
                                               merged.get("threshold"), reset)
    if not fields:
        raise HTTPException(400, "no fields to update")
    sets, params = [], {"id": rule_id}
    for k, v in fields.items():
        if k == "notify_channels":
            sets.append("notify_channels = CAST(:notify_channels AS jsonb)")
            params["notify_channels"] = _channels(v)
        elif k == "conditions":
            sets.append("conditions = CAST(:conditions AS jsonb)")
            params["conditions"] = v
        elif k == "escalation_policy_id":
            sets.append("escalation_policy_id = :escalation_policy_id")
            params[k] = await _policy_or_422(db, v)
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
