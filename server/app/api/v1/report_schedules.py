"""Scheduled-report CRUD + manual run.

Recurring report definitions (``report_schedules``) delivered by
``report_scheduler`` to notification channels and direct email recipients.
Full CRUD plus ``/{id}/run-now`` (fire immediately) and ``/{id}/toggle``
(enable/disable); every change is audited. A schedule delivers a built-in
template (any ``report_sections.REPORT_PRESETS`` key), an organisation
template (``custom`` + ``custom_report_id``) or a legacy report type, for a
rolling or calendar period (``report_periods``) in the organisation timezone.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core import scoping
from app.core.permissions import SUPERUSER_PERMISSION, has_permission
from app.core.security import get_role_permissions, require_permission
from app.models.user import User
from app.services import report_periods
from app.services.alert_schedule import get_configured_timezone
from app.services.audit_service import write_audit_log
from app.services.report_scheduler import (
    LEGACY_FORMATS,
    LEGACY_REPORT_TYPES,
    MAX_RECIPIENTS,
    TEMPLATE_FORMATS,
    compute_next_run,
    generate_and_deliver,
    normalize_recipients,
)

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


_FREQS = "daily|weekly|monthly"
ALL_FORMATS = tuple(dict.fromkeys(LEGACY_FORMATS + TEMPLATE_FORMATS))


def _presets() -> dict:
    # Imported on use: report_sections publishes the template catalog at the
    # end of its import, which itself imports this package's siblings.
    from app.services import report_sections
    return report_sections.REPORT_PRESETS


def is_known_report_type(value: str) -> bool:
    return value in LEGACY_REPORT_TYPES or value == "custom" or value in _presets()


def _check_report_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    value = value.strip()
    if not is_known_report_type(value):
        raise ValueError(f"Unknown report type '{value[:60]}': use a report template key, "
                         "'custom' with custom_report_id, or a legacy report type")
    return value


def _check_period(value: Optional[str]) -> Optional[str]:
    if value is not None and not report_periods.is_period(value):
        raise ValueError(f"Unknown period: use one of {', '.join(report_periods.PERIOD_IDS)}")
    return value


def _check_format(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in ALL_FORMATS:
        raise ValueError(f"Unknown format: use one of {', '.join(ALL_FORMATS)}")
    return value


def _check_recipients(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, str):
        value = [v for v in re.split(r"[,;\s]+", value) if v]
    if not isinstance(value, list):
        raise ValueError("recipients must be a list of email addresses")
    if len(value) > MAX_RECIPIENTS * 4:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per schedule")
    return normalize_recipients(value)


ReportType = Annotated[str, Field(max_length=50), AfterValidator(_check_report_type)]
Period = Annotated[str, AfterValidator(_check_period)]
Format = Annotated[str, AfterValidator(_check_format)]
Recipients = Annotated[list[str], BeforeValidator(_check_recipients)]


class ReportScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True
    report_type: ReportType = "executive_summary"
    period: Period = "last_24h"
    format: Format = "pdf"
    filters: dict = Field(default_factory=dict)
    frequency: str = Field("daily", pattern=f"^({_FREQS})$")
    hour: int = Field(8, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)
    day_of_week: Optional[int] = Field(None, ge=1, le=7)
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    notify_channels: list[str] = Field(default_factory=list)
    custom_report_id: Optional[str] = None
    recipients: Recipients = Field(default_factory=list)


class ReportScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None
    report_type: Optional[ReportType] = None
    period: Optional[Period] = None
    format: Optional[Format] = None
    filters: Optional[dict] = None
    frequency: Optional[str] = Field(None, pattern=f"^({_FREQS})$")
    hour: Optional[int] = Field(None, ge=0, le=23)
    minute: Optional[int] = Field(None, ge=0, le=59)
    day_of_week: Optional[int] = Field(None, ge=1, le=7)
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    notify_channels: Optional[list[str]] = None
    custom_report_id: Optional[str] = None
    recipients: Optional[Recipients] = None


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
        "recipients": list(getattr(row, "recipients", None) or []),
        "custom_report_id": str(row.custom_report_id) if getattr(row, "custom_report_id", None) else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


_SELECT = "SELECT * FROM report_schedules"
# Fields that decide what a schedule delivers and to whom (see update).
_DELIVERY_KEYS = frozenset({"report_type", "custom_report_id", "filters", "recipients",
                            "notify_channels", "period", "format"})
# Schedules with their owner's name and their organisation template's name
# and visibility, for listings.
_SELECT_DETAIL = (
    "SELECT s.*, COALESCE(NULLIF(u.full_name, ''), u.username) AS owner_name, "
    "c.name AS template_name, c.visibility AS template_visibility, "
    "c.created_by::text AS template_owner_id "
    "FROM report_schedules s "
    "LEFT JOIN users u ON u.id = s.created_by "
    "LEFT JOIN custom_reports c ON c.id = s.custom_report_id"
)


async def _validated_filters(filters: dict | None) -> dict:
    """``filters`` with its NetFlow scope validated and canonical (400 for an
    invalid scope or unknown key): a schedule can never store a scope its run
    would reject, and run as "success" with every NetFlow section blank.
    Device ids must be UUIDs; the listing's ``netflow_withheld`` marker is
    never stored."""
    out = {k: v for k, v in (filters or {}).items() if k != "netflow_withheld"}
    if "device_ids" in out:
        from app.services.report_render import parse_device_ids
        ids = parse_device_ids(out["device_ids"])
        if ids:
            out["device_ids"] = ids
        else:
            out.pop("device_ids")
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


async def _filters_for_update(db: AsyncSession, user: User, stored: dict | None, sent: dict | None) -> dict:
    """The filters to store on update. A caller who cannot see the stored
    NetFlow scope was sent it withheld (``netflow_withheld``), so whatever it
    sends back cannot replace that scope: the stored scope is kept."""
    stored = stored if isinstance(stored, dict) else {}
    if stored.get("netflow") is not None:
        from app.api.v1.netflow.access import visibility_for_user

        vis = await visibility_for_user(db, user)
        if not _netflow_scope_visible(stored["netflow"], vis):
            out = await _validated_filters({k: v for k, v in (sent or {}).items() if k != "netflow"})
            out["netflow"] = stored["netflow"]
            return out
    return await _validated_filters(sent)


async def _is_full_admin(db: AsyncSession, user: User) -> bool:
    return SUPERUSER_PERMISSION in await get_role_permissions(db, user.role)


def _check_format_for_type(report_type: str, fmt: str) -> None:
    if report_type in LEGACY_REPORT_TYPES:
        if fmt not in LEGACY_FORMATS:
            raise HTTPException(400, "Legacy reports are delivered as PDF, Excel or CSV (or no attachment)")
    elif fmt not in TEMPLATE_FORMATS:
        raise HTTPException(400, "Template reports are delivered as PDF, Excel or HTML (or no attachment)")


async def _readable_template_id(db: AsyncSession, user: User, custom_id: Optional[str]) -> str:
    """The canonical id of an organisation template the caller may read
    (400 when none is given, 404 when it does not exist or is private to
    someone else)."""
    from app.services import report_render

    if not custom_id:
        raise HTTPException(400, "Choose the organisation report template to deliver (custom_report_id)")
    row = await report_render.load_template(db, custom_id)
    if not report_render.can_read_template(row, user, await _is_full_admin(db, user)):
        raise HTTPException(404, "Report template not found")
    return str(row.id)


def _audit_meta(row, **extra) -> dict:
    return {"name": row.name, "report_type": row.report_type, "period": row.period,
            "format": row.format, "recipients": len(getattr(row, "recipients", None) or []), **extra}


async def _audit(db: AsyncSession, user: User, action: str, row, **extra) -> None:
    await write_audit_log(db, actor=user, action=f"reports.schedule.{action}",
                          resource_type="report_schedule", resource_id=str(row.id),
                          metadata=_audit_meta(row, **extra))


def _report_title(row, user: User, admin: bool) -> str:
    """What the schedule delivers, by name."""
    rt = row.report_type
    if rt == "custom":
        name = getattr(row, "template_name", None)
        if not name:
            return "Deleted template"
        private_to_other = getattr(row, "template_visibility", None) == "private" and not admin \
            and str(getattr(row, "template_owner_id", None) or "") != str(getattr(user, "id", ""))
        return "Private template" if private_to_other else name
    if rt in LEGACY_REPORT_TYPES:
        from app.services import report_html
        return report_html.report_title(rt)
    preset = _presets().get(rt)
    return preset["title"] if preset else rt


class _Viewer:
    """Per-request facts for serialising schedules for ``user``."""

    def __init__(self, user: User, admin: bool, manager: bool, vis):
        self.user, self.admin, self.manager, self.vis = user, admin, manager, vis

    @classmethod
    async def load(cls, db: AsyncSession, user: User) -> "_Viewer":
        from app.api.v1.netflow.access import visibility_for_user

        return cls(user, await _is_full_admin(db, user), await is_report_manager(db, user),
                   await visibility_for_user(db, user))

    def serialize(self, row) -> dict:
        item = _row_to_dict(row)
        filters = item.get("filters") or {}
        if isinstance(filters, dict) and "netflow" in filters and not _netflow_scope_visible(filters["netflow"], self.vis):
            # The scope names hosts or exporters the caller may not see (or
            # the caller has no NetFlow access): withhold it, say so.
            item["filters"] = {**{k: v for k, v in filters.items() if k != "netflow"}, "netflow_withheld": True}
        owner_id = getattr(row, "created_by", None)
        item["owner_id"] = str(owner_id) if owner_id else None
        item["owner_name"] = getattr(row, "owner_name", None)
        item["report_title"] = _report_title(row, self.user, self.admin)
        # Writes need reports.manage outside a visibility scope (a report
        # manager); an owner without that cannot change its schedule either.
        item["can_edit"] = self.manager
        item["is_owner"] = owner_id is not None and str(owner_id) == str(getattr(self.user, "id", ""))
        return item


async def _load_detail(db: AsyncSession, schedule_id) -> Any:
    return (await db.execute(text(f"{_SELECT_DETAIL} WHERE s.id = :id"), {"id": schedule_id})).first()


@router.get("")
async def list_report_schedules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.view")),
):
    rows = (await db.execute(text(f"{_SELECT_DETAIL} ORDER BY s.created_at DESC"))).fetchall()
    viewer = await _Viewer.load(db, user)
    return {"data": [viewer.serialize(r) for r in rows]}


@router.get("/{schedule_id}")
async def get_report_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.view")),
):
    row = await _load_detail(db, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    return (await _Viewer.load(db, user)).serialize(row)


@router.post("", status_code=201)
async def create_report_schedule(
    data: ReportScheduleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    _check_format_for_type(data.report_type, data.format)
    if data.report_type == "custom":
        data.custom_report_id = await _readable_template_id(db, user, data.custom_report_id)
    else:
        data.custom_report_id = None
    data.filters = await _validated_filters(data.filters)
    tz_name = await get_configured_timezone(db)
    sched = data.model_dump()
    next_run = compute_next_run(sched, tz_name) if data.enabled else None
    row = (await db.execute(
        text("""INSERT INTO report_schedules
                (name, description, enabled, report_type, period, format, filters,
                 frequency, hour, minute, day_of_week, day_of_month, notify_channels,
                 recipients, custom_report_id, next_run_at, created_by, created_at, updated_at)
                VALUES (:name, :description, :enabled, :report_type, :period, :format,
                        CAST(:filters AS jsonb), :frequency, :hour, :minute,
                        :day_of_week, :day_of_month, CAST(:notify_channels AS jsonb),
                        CAST(:recipients AS jsonb), :custom_report_id, :next_run_at, :created_by, NOW(), NOW())
                RETURNING *"""),
        {
            "name": data.name, "description": data.description, "enabled": data.enabled,
            "report_type": data.report_type, "period": data.period, "format": data.format,
            "filters": json.dumps(data.filters or {}), "frequency": data.frequency,
            "custom_report_id": data.custom_report_id,
            "hour": data.hour, "minute": data.minute, "day_of_week": data.day_of_week,
            "day_of_month": data.day_of_month,
            "notify_channels": json.dumps(data.notify_channels or []),
            "recipients": json.dumps(data.recipients or []),
            "next_run_at": next_run, "created_by": getattr(user, "id", None),
        },
    )).first()
    await _audit(db, user, "create", row)
    await db.commit()
    detail = await _load_detail(db, row.id)
    return (await _Viewer.load(db, user)).serialize(detail or row)


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
    current = (await db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": schedule_id})).first()
    if not current:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    for key in ("report_type", "period", "format", "name", "enabled", "frequency", "hour", "minute"):
        if key in fields and fields[key] is None:
            raise HTTPException(status_code=400, detail=f"'{key}' cannot be empty")

    report_type = fields.get("report_type", current.report_type)
    if "report_type" in fields or "format" in fields:
        _check_format_for_type(report_type, fields.get("format", current.format))
    if report_type == "custom":
        if "report_type" in fields or "custom_report_id" in fields:
            requested = fields["custom_report_id"] if "custom_report_id" in fields \
                else getattr(current, "custom_report_id", None)
            fields["custom_report_id"] = await _readable_template_id(
                db, user, str(requested) if requested else None)
    elif getattr(current, "custom_report_id", None) or "custom_report_id" in fields:
        fields["custom_report_id"] = None
    if "filters" in fields:
        fields["filters"] = await _filters_for_update(db, user, current.filters, fields["filters"])

    # A schedule renders with its owner's permissions. Someone else who changes
    # what it delivers, or to whom, takes it over: from then on it runs with the
    # editor's permissions, so a report manager cannot use another owner's
    # broader access to send data they may not see to recipients they choose.
    owner_changed = False
    if (_DELIVERY_KEYS & set(fields)
            and str(getattr(current, "created_by", "") or "") != str(user.id)
            and not await _is_full_admin(db, user)):
        fields["created_by"] = user.id
        owner_changed = True

    set_parts, params = [], {"id": schedule_id}
    for key, value in fields.items():
        if key == "filters":
            set_parts.append("filters = CAST(:filters AS jsonb)")
            params[key] = json.dumps(value or {})
        elif key in ("notify_channels", "recipients"):
            set_parts.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(value or [])
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
    await _audit(db, user, "update", row, changed=sorted(k for k in fields if k != "created_by"),
                 **({"owner_changed_from": str(current.created_by) if current.created_by else None}
                    if owner_changed else {}))
    await db.commit()
    detail = await _load_detail(db, schedule_id)
    return (await _Viewer.load(db, user)).serialize(detail or row)


@router.delete("/{schedule_id}", status_code=204)
async def delete_report_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    row = (await db.execute(
        text("DELETE FROM report_schedules WHERE id = :id RETURNING *"), {"id": schedule_id}
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    await _audit(db, user, "delete", row)
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
    await _audit(db, user, "toggle", row, enabled=new_enabled)
    await db.commit()
    detail = await _load_detail(db, schedule_id)
    return (await _Viewer.load(db, user)).serialize(detail or row)


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
    await _audit(db, user, "run_now", row, status=result["status"], delivered=len(result["delivered"]))
    await db.commit()
    message = (f"Report generated ({result['status']})" if result.get("run_id")
               else f"Report not generated: {'; '.join(result['errors'])}")
    return {"message": message, **result}


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
