"""Scheduled-report CRUD + manual run.

Recurring report definitions (``report_schedules``) delivered to notification
channels by ``report_scheduler``. Full CRUD plus ``/{id}/run-now`` (fire
immediately) and ``/{id}/toggle`` (enable/disable). Listing also returns recent
run history so the UI can show last-delivery status and share links.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core import scoping
from app.core.permissions import has_permission
from app.core.security import get_current_user, get_role_permissions, require_admin_user, require_permission
from app.models.user import User
from app.services.alert_schedule import get_configured_timezone
from app.services.report_scheduler import compute_next_run, generate_and_deliver

router = APIRouter(prefix="/report-schedules", tags=["Report Schedules"])


async def _refuse_scoped(db: AsyncSession, user: User) -> None:
    """Scheduled reports render fleet-wide data without a user context and email
    it onward, so an account limited to a visibility scope may not create or
    change them (fail closed)."""
    if await scoping.visible_tags(db, user) is not None:
        raise HTTPException(
            status_code=403,
            detail="Accounts limited to a visibility scope cannot manage report schedules",
        )

_REPORT_TYPES = ("executive_summary|device_health|service_health|alert_analysis|full_report"
                 "|availability|performance|traffic|usage|apm_performance"
                 "|capacity|alerts|inventory|rum_usage|ncm_protection|netflow_executive|custom")
_PERIODS = "last_24h|last_7d|last_30d|last_90d"
_FORMATS = "pdf|excel|csv|none"
_FREQS = "daily|weekly|monthly"


class ReportScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True
    report_type: str = Field("executive_summary", pattern=f"^({_REPORT_TYPES})$")
    period: str = Field("last_24h", pattern=f"^({_PERIODS})$")
    format: str = Field("pdf", pattern=f"^({_FORMATS})$")
    filters: dict = Field(default_factory=dict)
    frequency: str = Field("daily", pattern=f"^({_FREQS})$")
    hour: int = Field(8, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)
    day_of_week: Optional[int] = Field(None, ge=1, le=7)
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    notify_channels: list[str] = Field(default_factory=list)
    custom_report_id: Optional[str] = None


class ReportScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None
    report_type: Optional[str] = Field(None, pattern=f"^({_REPORT_TYPES})$")
    period: Optional[str] = Field(None, pattern=f"^({_PERIODS})$")
    format: Optional[str] = Field(None, pattern=f"^({_FORMATS})$")
    filters: Optional[dict] = None
    frequency: Optional[str] = Field(None, pattern=f"^({_FREQS})$")
    hour: Optional[int] = Field(None, ge=0, le=23)
    minute: Optional[int] = Field(None, ge=0, le=59)
    day_of_week: Optional[int] = Field(None, ge=1, le=7)
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    notify_channels: Optional[list[str]] = None
    custom_report_id: Optional[str] = None


def _row_to_dict(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "report_type": row.report_type,
        "period": row.period,
        "format": row.format,
        "filters": row.filters or {},
        "frequency": row.frequency,
        "hour": row.hour,
        "minute": row.minute,
        "day_of_week": row.day_of_week,
        "day_of_month": row.day_of_month,
        "notify_channels": row.notify_channels or [],
        "custom_report_id": str(row.custom_report_id) if getattr(row, "custom_report_id", None) else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


_SELECT = "SELECT * FROM report_schedules"


async def _validated_filters(filters: dict | None) -> dict:
    """``filters`` with its NetFlow scope validated and canonical (400 for an
    invalid scope or unknown key): a schedule can never store a scope its run
    would reject, and run as "success" with every NetFlow section blank."""
    out = dict(filters or {})
    if "netflow" not in out:
        return out
    from app.api.v1.netflow.common import NetflowError
    from app.services import netflow_classify, netflow_groups, netflow_report

    await netflow_classify.load_rules()
    await netflow_groups.load_groups()
    try:
        scope = netflow_report.scope_from_mapping(out["netflow"])
    except NetflowError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid NetFlow scope: {exc.detail}") from None
    if scope:
        out["netflow"] = scope
    else:
        out.pop("netflow")
    return out


_HOST_KEYS = ("talker", "src", "dst", "not_talker", "not_src", "not_dst")


def _netflow_scope_visible(scope, vis) -> bool:
    """Whether a caller may see a schedule's NetFlow scope: it needs
    netflow.view; a tag-scoped caller only sees scopes whose exporters it can
    see, and host filters only when they are pinned to such an exporter."""
    from app.api.v1.netflow.access import VIEW_PERMISSION

    if not vis.can(VIEW_PERMISSION):
        return False
    if vis.exporters is None:
        return True
    if not isinstance(scope, dict):
        return False
    named = [str(scope["exporter"])] if scope.get("exporter") else []
    iface = str(scope.get("iface") or "")
    if "@" in iface:
        named.append(iface.split("@", 1)[1])
    if any(not vis.allows(e) for e in named):
        return False
    return bool(named) or not any(scope.get(k) for k in _HOST_KEYS)


@router.get("")
async def list_report_schedules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.view")),
):
    from app.api.v1.netflow.access import visibility_for_user

    rows = (await db.execute(text(f"{_SELECT} ORDER BY created_at DESC"))).fetchall()
    vis = await visibility_for_user(db, user)
    out = []
    for r in rows:
        item = _row_to_dict(r)
        filters = item.get("filters") or {}
        if isinstance(filters, dict) and "netflow" in filters and not _netflow_scope_visible(filters["netflow"], vis):
            # The scope names hosts or exporters the caller may not see (or
            # the caller has no NetFlow access): withhold it, say so.
            item["filters"] = {**{k: v for k, v in filters.items() if k != "netflow"}, "netflow_withheld": True}
        out.append(item)
    return {"data": out}


@router.post("", status_code=201)
async def create_report_schedule(
    data: ReportScheduleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    data.filters = await _validated_filters(data.filters)
    tz_name = await get_configured_timezone(db)
    sched = data.model_dump()
    next_run = compute_next_run(sched, tz_name) if data.enabled else None
    row = (await db.execute(
        text("""INSERT INTO report_schedules
                (name, description, enabled, report_type, period, format, filters,
                 frequency, hour, minute, day_of_week, day_of_month, notify_channels,
                 custom_report_id, next_run_at, created_by, created_at, updated_at)
                VALUES (:name, :description, :enabled, :report_type, :period, :format,
                        CAST(:filters AS jsonb), :frequency, :hour, :minute,
                        :day_of_week, :day_of_month, CAST(:notify_channels AS jsonb),
                        :custom_report_id, :next_run_at, :created_by, NOW(), NOW())
                RETURNING *"""),
        {
            "name": data.name, "description": data.description, "enabled": data.enabled,
            "report_type": data.report_type, "period": data.period, "format": data.format,
            "filters": json.dumps(data.filters or {}), "frequency": data.frequency,
            "custom_report_id": data.custom_report_id,
            "hour": data.hour, "minute": data.minute, "day_of_week": data.day_of_week,
            "day_of_month": data.day_of_month,
            "notify_channels": json.dumps(data.notify_channels or []),
            "next_run_at": next_run, "created_by": getattr(user, "id", None),
        },
    )).first()
    await db.commit()
    return _row_to_dict(row)


@router.put("/{schedule_id}")
async def update_report_schedule(
    schedule_id: UUID,
    data: ReportScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "filters" in fields:
        fields["filters"] = await _validated_filters(fields["filters"])

    set_parts, params = [], {"id": schedule_id}
    for key, value in fields.items():
        if key in ("filters",):
            set_parts.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(value or {})
        elif key == "notify_channels":
            set_parts.append("notify_channels = CAST(:notify_channels AS jsonb)")
            params["notify_channels"] = json.dumps(value or [])
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value
    set_parts.append("updated_at = NOW()")

    row = (await db.execute(
        text(f"UPDATE report_schedules SET {', '.join(set_parts)} WHERE id = :id RETURNING *"),
        params,
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")

    # Recompute next_run whenever timing/enabled changed.
    timing_keys = {"enabled", "frequency", "hour", "minute", "day_of_week", "day_of_month"}
    if timing_keys & set(fields.keys()):
        tz_name = await get_configured_timezone(db)
        sched = _row_to_dict(row)
        next_run = compute_next_run(sched, tz_name) if row.enabled else None
        row = (await db.execute(
            text("UPDATE report_schedules SET next_run_at = :n WHERE id = :id RETURNING *"),
            {"n": next_run, "id": schedule_id},
        )).first()
    await db.commit()
    return _row_to_dict(row)


@router.delete("/{schedule_id}", status_code=204)
async def delete_report_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    row = (await db.execute(
        text("DELETE FROM report_schedules WHERE id = :id RETURNING id"), {"id": schedule_id}
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    await db.commit()


@router.post("/{schedule_id}/toggle")
async def toggle_report_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    row = (await db.execute(
        text(f"{_SELECT} WHERE id = :id"), {"id": schedule_id}
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    new_enabled = not row.enabled
    tz_name = await get_configured_timezone(db)
    sched = _row_to_dict(row)
    sched["enabled"] = new_enabled
    next_run = compute_next_run(sched, tz_name) if new_enabled else None
    row = (await db.execute(
        text("UPDATE report_schedules SET enabled = :e, next_run_at = :n, updated_at = NOW() "
             "WHERE id = :id RETURNING *"),
        {"e": new_enabled, "n": next_run, "id": schedule_id},
    )).first()
    await db.commit()
    return _row_to_dict(row)


@router.post("/{schedule_id}/run-now")
async def run_report_schedule_now(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    row = (await db.execute(
        text(f"{_SELECT} WHERE id = :id"), {"id": schedule_id}
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    sched = _row_to_dict(row)
    result = await generate_and_deliver(db, sched, triggered_by="manual")
    await db.execute(
        text("UPDATE report_schedules SET last_run_at = NOW(), last_status = :st, last_error = :err, "
             "updated_at = NOW() WHERE id = :id"),
        {"st": result["status"], "err": "; ".join(result["errors"]) or None, "id": schedule_id},
    )
    await db.commit()
    return {"message": f"Report generated ({result['status']})", **result}


REPORTS_VIEW_PERMISSION = "reports.view"
REPORTS_MANAGE_PERMISSION = "reports.manage"


async def is_report_manager(db: AsyncSession, user: User) -> bool:
    """A reports.manage holder who may actually manage report schedules: an
    account limited to a visibility scope cannot (``_refuse_scoped``), so it
    is not a report manager here either -- its permission alone must not turn
    it into a reader of fleet-wide scheduled reports."""
    if not has_permission(await get_role_permissions(db, user.role), REPORTS_MANAGE_PERMISSION):
        return False
    return await scoping.visible_tags(db, user) is None


async def sees_share_links(db: AsyncSession, user: User, owner_id) -> bool:
    """Whether a run's share token may be shown to ``user``.

    The token opens the stored report without signing in, and a scheduled
    report renders with its owner's visibility (NetFlow sections included),
    so a token is a copy of what the owner may see: only the schedule's
    owner (``report_schedules.created_by``) and report managers
    (``is_report_manager``) get it. Everyone else sees the run without its
    link."""
    if await is_report_manager(db, user):
        return True
    return owner_id is not None and str(owner_id) == str(getattr(user, "id", ""))


@router.get("/{schedule_id}/runs")
async def list_runs(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(REPORTS_VIEW_PERMISSION)),
):
    """A schedule's recent runs. Share tokens only for the schedule's owner
    and report managers (``sees_share_links``)."""
    schedule = (await db.execute(
        text("SELECT created_by FROM report_schedules WHERE id = :id"), {"id": schedule_id})).first()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    links = await sees_share_links(db, user, schedule.created_by)
    rows = (await db.execute(
        text("""SELECT id, token, title, status, delivered_to, error, generated_at
                FROM report_runs WHERE schedule_id = :id ORDER BY generated_at DESC LIMIT 20"""),
        {"id": schedule_id},
    )).fetchall()
    return {"data": [
        {
            "id": str(r.id), "token": r.token if links else None, "token_withheld": not links,
            "title": r.title, "status": r.status,
            "delivered_to": r.delivered_to or [], "error": r.error,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        } for r in rows
    ], "can_view_links": links}
