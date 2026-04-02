import json
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/alert-rules", tags=["Alert Rules"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True

    metric: str = Field(..., pattern="^(ping_status|rtt|packet_loss|jitter)$")
    operator: str = Field(..., pattern="^(>|<|>=|<=|==|!=)$")
    threshold: float

    trigger_on: str = Field(default="any", pattern="^(any|down|up|degraded)$")
    recovery_alert: bool = False

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None

    severity: str = Field(default="warning", pattern="^(info|warning|critical)$")
    notify_channels: list[str] = Field(default_factory=list)
    cooldown: int = Field(default=300, ge=0)
    min_duration: int = Field(default=0, ge=0)
    max_repeat: int = Field(default=0, ge=0)

    schedule_start: Optional[str] = None  # HH:MM
    schedule_end: Optional[str] = None    # HH:MM
    schedule_days: Optional[list[int]] = None  # 1-7 (Mon-Sun)


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None

    metric: Optional[str] = Field(None, pattern="^(ping_status|rtt|packet_loss|jitter)$")
    operator: Optional[str] = Field(None, pattern="^(>|<|>=|<=|==|!=)$")
    threshold: Optional[float] = None

    trigger_on: Optional[str] = Field(None, pattern="^(any|down|up|degraded)$")
    recovery_alert: Optional[bool] = None

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None

    severity: Optional[str] = Field(None, pattern="^(info|warning|critical)$")
    notify_channels: Optional[list[str]] = None
    cooldown: Optional[int] = Field(None, ge=0)
    min_duration: Optional[int] = Field(None, ge=0)
    max_repeat: Optional[int] = Field(None, ge=0)

    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    schedule_days: Optional[list[int]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Columns to SELECT in list / get queries
_RULE_COLUMNS = (
    "id, name, description, enabled, metric, operator, threshold, duration, "
    "device_id, group_id, severity, notify_channels, cooldown, "
    "device_type, location, trigger_on, recovery_alert, "
    "min_duration, max_repeat, schedule_start, schedule_end, schedule_days, "
    "created_at, updated_at, created_by"
)


def _row_to_dict(row) -> dict:
    schedule_start = row.schedule_start
    schedule_end = row.schedule_end
    if schedule_start is not None and not isinstance(schedule_start, str):
        schedule_start = schedule_start.strftime("%H:%M")
    if schedule_end is not None and not isinstance(schedule_end, str):
        schedule_end = schedule_end.strftime("%H:%M")

    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "metric": row.metric,
        "operator": row.operator,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "duration": row.duration,
        "device_id": str(row.device_id) if row.device_id else None,
        "group_id": str(row.group_id) if row.group_id else None,
        "severity": row.severity,
        "notify_channels": row.notify_channels if row.notify_channels else [],
        "cooldown": row.cooldown,
        "device_type": row.device_type,
        "location": row.location,
        "trigger_on": row.trigger_on,
        "recovery_alert": row.recovery_alert,
        "min_duration": row.min_duration,
        "max_repeat": row.max_repeat,
        "schedule_start": schedule_start,
        "schedule_end": schedule_end,
        "schedule_days": row.schedule_days if row.schedule_days else [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_alert_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules ORDER BY created_at DESC")
    )
    rows = result.fetchall()
    return {"data": [_row_to_dict(r) for r in rows]}


@router.post("", status_code=201)
async def create_alert_rule(
    data: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    params: dict = {
        "name": data.name,
        "description": data.description,
        "enabled": data.enabled,
        "metric": data.metric,
        "operator": data.operator,
        "threshold": data.threshold,
        "duration": data.min_duration,  # legacy column maps to min_duration
        "device_id": data.device_id,
        "group_id": data.group_id,
        "severity": data.severity,
        "notify_channels": json.dumps(data.notify_channels),
        "cooldown": data.cooldown,
        "device_type": data.device_type,
        "location": data.location,
        "trigger_on": data.trigger_on,
        "recovery_alert": data.recovery_alert,
        "min_duration": data.min_duration,
        "max_repeat": data.max_repeat,
        "schedule_start": data.schedule_start,
        "schedule_end": data.schedule_end,
        "schedule_days": json.dumps(data.schedule_days) if data.schedule_days else None,
        "created_at": now,
        "updated_at": now,
        "created_by": user.id,
    }

    result = await db.execute(
        text(
            "INSERT INTO alert_rules "
            "(name, description, enabled, metric, operator, threshold, duration, "
            "device_id, group_id, severity, notify_channels, cooldown, "
            "device_type, location, trigger_on, recovery_alert, "
            "min_duration, max_repeat, schedule_start, schedule_end, schedule_days, "
            "created_at, updated_at, created_by) "
            "VALUES "
            "(:name, :description, :enabled, :metric, :operator, :threshold, :duration, "
            ":device_id, :group_id, :severity, CAST(:notify_channels AS jsonb), :cooldown, "
            ":device_type, :location, :trigger_on, :recovery_alert, "
            ":min_duration, :max_repeat, CAST(:schedule_start AS time), CAST(:schedule_end AS time), "
            "CAST(:schedule_days AS jsonb), :created_at, :updated_at, :created_by) "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create alert rule")
    return _row_to_dict(row)


@router.get("/{rule_id}")
async def get_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _row_to_dict(row)


@router.put("/{rule_id}")
async def update_alert_rule(
    rule_id: UUID,
    data: AlertRuleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    params: dict = {"id": rule_id, "updated_at": datetime.now(timezone.utc)}

    # Special-case columns that need casts
    _jsonb_cols = {"notify_channels", "schedule_days"}
    _time_cols = {"schedule_start", "schedule_end"}

    for key, value in fields.items():
        if key in _jsonb_cols:
            set_parts.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(value) if value is not None else None
        elif key in _time_cols:
            set_parts.append(f"{key} = CAST(:{key} AS time)")
            params[key] = value
        elif key == "device_id" or key == "group_id":
            set_parts.append(f"{key} = :{key}")
            params[key] = value
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    # Keep legacy duration column in sync with min_duration
    if "min_duration" in fields:
        set_parts.append("duration = :min_duration_dup")
        params["min_duration_dup"] = fields["min_duration"]

    set_parts.append("updated_at = :updated_at")
    set_clause = ", ".join(set_parts)

    result = await db.execute(
        text(
            f"UPDATE alert_rules SET {set_clause} "
            f"WHERE id = :id "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _row_to_dict(row)


@router.delete("/{rule_id}", status_code=204)
async def delete_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("DELETE FROM alert_rules WHERE id = :id RETURNING id"),
        {"id": rule_id},
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")


@router.post("/{rule_id}/toggle")
async def toggle_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(
            "UPDATE alert_rules SET enabled = NOT enabled, updated_at = :now "
            "WHERE id = :id "
            "RETURNING id, name, enabled"
        ),
        {"id": rule_id, "now": datetime.now(timezone.utc)},
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return {
        "id": str(row.id),
        "name": row.name,
        "enabled": row.enabled,
        "message": f"Rule '{ row.name }' {'enabled' if row.enabled else 'disabled'}",
    }
