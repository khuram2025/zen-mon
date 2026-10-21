"""Scheduled-report CRUD + manual run.

Recurring report definitions (``report_schedules``) delivered by
``report_scheduler`` to notification channels and direct email recipients.
Full CRUD plus ``/{id}/run-now`` (fire immediately) and ``/{id}/toggle``
(enable/disable); every change is audited. A schedule delivers a built-in
template (any ``report_sections.REPORT_PRESETS`` key), an organisation
template (``custom`` + ``custom_report_id``) or a legacy report type, for a
rolling or calendar period (``report_periods``).

A schedule's hour and minute are wall-clock time in its own IANA ``timezone``
(migrate-163), which defaults to the organisation timezone when the schedule
is created; changing the organisation timezone later does not move it. Every
schedule is returned with ``next_run_at`` (UTC), its ``effective_timezone``
and a ``local_time_label`` such as "08:00 Asia/Riyadh (05:00 UTC)".
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

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
from app.services.branding import valid_timezone
from app.services.report_scheduler import (
    LEGACY_FORMATS,
    LEGACY_REPORT_TYPES,
    MAX_RECIPIENTS,
    TEMPLATE_FORMATS,
    compute_next_run,
    generate_and_deliver,
    local_time_label,
    name_time_mismatch,
    is_email,
    normalize_recipients,
    schedule_text,
    schedule_timezone,
    suggested_time,
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
# Retired legacy report types and the built-in template that replaces each
# (docs/reports-delivery.md): existing schedules keep running and can be
# converted; new ones cannot be created.
LEGACY_REPLACEMENTS = {
    "executive_summary": "executive_overview",
    "device_health": "availability",
    "service_health": "service_owner_sla",
    "alert_analysis": "alerts",
    "full_report": "network_operations",
}
# Scope keys a schedule may store (anything else is refused).
FILTER_KEYS = frozenset({"device_ids", "group_ids", "locations", "device_types", "netflow", "application_id"})
OVERDUE_AFTER_S = 600


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


def _check_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    value = value.strip()
    if not value:
        raise ValueError("Name is required")
    return value


def _check_reply_to(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if not is_email(value):
        raise ValueError(f"'{value[:80]}' is not a valid reply-to address")
    return value


def _check_subject(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _check_timezone(value: Optional[str]) -> Optional[str]:
    """An IANA zone name, trimmed; blank means "the organisation timezone"."""
    if value is None:
        return None
    name = value.strip()
    if not name:
        return None
    if len(name) > 64 or not valid_timezone(name):
        raise ValueError(f"Unknown timezone '{name[:64]}': use an IANA name such as Asia/Riyadh or Europe/Berlin")
    return name


ReportType = Annotated[str, Field(max_length=50), AfterValidator(_check_report_type)]
Period = Annotated[str, AfterValidator(_check_period)]
Format = Annotated[str, AfterValidator(_check_format)]
Recipients = Annotated[list[str], BeforeValidator(_check_recipients)]
TimezoneName = Annotated[str, Field(max_length=100), AfterValidator(_check_timezone)]
Name = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_check_name)]
ReplyTo = Annotated[str, Field(max_length=254), AfterValidator(_check_reply_to)]
SubjectTemplate = Annotated[str, Field(max_length=255), AfterValidator(_check_subject)]


class ReportScheduleCreate(BaseModel):
    name: Name
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
    # The zone hour/minute are in; omitted or blank = the organisation timezone.
    timezone: Optional[TimezoneName] = None
    notify_channels: list[str] = Field(default_factory=list)
    custom_report_id: Optional[str] = None
    recipients: Recipients = Field(default_factory=list)
    skip_when_empty: bool = False
    subject_template: Optional[SubjectTemplate] = None
    reply_to: Optional[ReplyTo] = None
    group_send: bool = False
    cc: Recipients = Field(default_factory=list)


class ReportScheduleUpdate(BaseModel):
    name: Optional[Name] = None
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
    # null or blank = the organisation timezone (stored explicitly).
    timezone: Optional[TimezoneName] = None
    notify_channels: Optional[list[str]] = None
    custom_report_id: Optional[str] = None
    recipients: Optional[Recipients] = None
    skip_when_empty: Optional[bool] = None
    subject_template: Optional[SubjectTemplate] = None
    reply_to: Optional[ReplyTo] = None
    group_send: Optional[bool] = None
    cc: Optional[Recipients] = None


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
        "timezone": getattr(row, "timezone", None),
        "notify_channels": row.notify_channels or [],
        "recipients": list(getattr(row, "recipients", None) or []),
        "skip_when_empty": bool(getattr(row, "skip_when_empty", False)),
        "subject_template": getattr(row, "subject_template", None),
        "reply_to": getattr(row, "reply_to", None),
        "group_send": bool(getattr(row, "group_send", False)),
        "cc": list(getattr(row, "cc", None) or []),
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
                            "notify_channels", "period", "format", "cc", "group_send"})
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
    unknown = sorted(k for k in out if k not in FILTER_KEYS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown scope filter(s): {', '.join(unknown)[:200]}; "
                                                    f"use {', '.join(sorted(FILTER_KEYS))}")
    for key in ("group_ids", "locations", "device_types"):
        if key in out:
            vals = out[key]
            if isinstance(vals, str):
                vals = [v for v in re.split(r"[,;]", vals)]
            if not isinstance(vals, list):
                raise HTTPException(status_code=400, detail=f"{key} must be a list")
            vals = list(dict.fromkeys(str(v).strip() for v in vals if str(v).strip()))[:200]
            if any(len(v) > 128 for v in vals):
                raise HTTPException(status_code=400, detail=f"{key} values are at most 128 characters")
            if vals:
                out[key] = vals
            else:
                out.pop(key)
    if "application_id" in out:
        app_id = str(out["application_id"] or "").strip()
        if app_id and len(app_id) <= 128:
            out["application_id"] = app_id
        else:
            out.pop("application_id")
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
        raise HTTPException(400, "Template reports are delivered as PDF, Excel, CSV or HTML (or no attachment)")


def _normalize_timing(fields: dict, frequency: str) -> None:
    """Weekly needs its weekday and monthly its day of the month (they used
    to fall back to Monday / the 1st silently); fields that do not apply to
    the frequency are cleared."""
    if frequency == "weekly":
        if fields.get("day_of_week") is None:
            raise HTTPException(400, "Weekly schedules need day_of_week (1 = Monday … 7 = Sunday)")
        fields["day_of_month"] = None
    elif frequency == "monthly":
        if fields.get("day_of_month") is None:
            raise HTTPException(400, "Monthly schedules need day_of_month (1–31; 29–31 run on the "
                                     "month's last day in shorter months)")
        fields["day_of_week"] = None
    else:
        fields["day_of_week"] = None
        fields["day_of_month"] = None


async def _check_channels(db: AsyncSession, channel_ids: list) -> list[str]:
    """Canonical ids of existing, enabled notification channels; 400 naming
    anything else (a bad id used to fail every run with a database error)."""
    out: list[str] = []
    bad: list[str] = []
    for raw in channel_ids or []:
        try:
            cid = str(UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            bad.append(str(raw)[:40])
            continue
        if cid not in out:
            out.append(cid)
    if bad:
        raise HTTPException(400, f"Not a notification channel id: {', '.join(bad)}")
    if out:
        rows = (await db.execute(
            text("SELECT id::text, enabled FROM notification_channels WHERE id::text = ANY(:ids)"),
            {"ids": out})).fetchall()
        found = {r[0]: r[1] for r in rows}
        missing = [c for c in out if c not in found]
        disabled = [c for c in out if found.get(c) is False]
        if missing:
            raise HTTPException(400, f"Notification channel(s) not found: {', '.join(missing)}")
        if disabled:
            raise HTTPException(400, f"Notification channel(s) are disabled: {', '.join(disabled)}")
    return out


def _check_email_options(fields: dict, current: Any = None) -> None:
    group = fields.get("group_send", getattr(current, "group_send", False))
    cc = fields.get("cc", list(getattr(current, "cc", None) or []))
    recipients = fields.get("recipients", list(getattr(current, "recipients", None) or []))
    if cc and not group:
        raise HTTPException(400, "CC only applies when one email goes to all recipients (group_send)")
    if cc and not recipients:
        raise HTTPException(400, "CC needs at least one direct recipient")


async def _share_links_available(db: AsyncSession) -> bool:
    from app.services.branding import load_branding
    from app.services.report_scheduler import _base_url
    return bool(_base_url(await load_branding(db)))


async def _check_summary_only(db: AsyncSession, fmt: Optional[str]) -> None:
    """"Summary only" promises a link to the full report: refuse it while no
    base URL is configured (the email would carry neither file nor link)."""
    if fmt == "none" and not await _share_links_available(db):
        raise HTTPException(400, "\"Summary only\" needs a link to the report, and no base URL is "
                                 "configured: set it in Settings → Branding, or attach the report")


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
    """What the schedule delivers and when: the timing fields make a later
    "why did it run at 05:00?" answerable from the audit log alone."""
    timing = {"frequency": getattr(row, "frequency", None), "hour": getattr(row, "hour", None),
              "minute": getattr(row, "minute", None), "timezone": getattr(row, "timezone", None)}
    if timing["frequency"] == "weekly":
        timing["day_of_week"] = getattr(row, "day_of_week", None)
    elif timing["frequency"] == "monthly":
        timing["day_of_month"] = getattr(row, "day_of_month", None)
    return {"name": row.name, "report_type": row.report_type, "period": row.period,
            "format": row.format, "recipients": len(getattr(row, "recipients", None) or []),
            **timing, **extra}


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


def timing_fields(item: dict, org_tz: str) -> dict:
    """Derived timing facts of a serialised schedule: the zone it runs in, its
    local time with the UTC equivalent, the full wording, the next run in
    local time, and the time written in its name when that differs."""
    zone = schedule_timezone(item, org_tz)
    out = {
        "effective_timezone": zone,
        "organisation_timezone": org_tz,
        "local_time_label": local_time_label(item, org_tz),
        "schedule_label": schedule_text(item, org_tz),
        "next_run_local": None,
        "name_time_mismatch": name_time_mismatch(item.get("name"), item.get("hour"), item.get("minute")),
        "suggested_time": suggested_time(item.get("name"), item.get("hour"), item.get("minute")),
        "overdue": False,
    }
    nxt = item.get("next_run_at")
    if nxt:
        try:
            due = datetime.fromisoformat(nxt)
            out["next_run_local"] = due.astimezone(ZoneInfo(zone)).isoformat()
            out["overdue"] = bool(item.get("enabled")) and item.get("last_status") != "running" and \
                (datetime.now(timezone.utc) - due).total_seconds() > OVERDUE_AFTER_S
        except (TypeError, ValueError):
            pass
    return out


class _Viewer:
    """Per-request facts for serialising schedules for ``user``."""

    def __init__(self, user: User, admin: bool, manager: bool, vis, org_tz: str = "UTC"):
        self.user, self.admin, self.manager, self.vis = user, admin, manager, vis
        self.org_tz = org_tz

    @classmethod
    async def load(cls, db: AsyncSession, user: User) -> "_Viewer":
        from app.api.v1.netflow.access import visibility_for_user

        return cls(user, await _is_full_admin(db, user), await is_report_manager(db, user),
                   await visibility_for_user(db, user), await get_configured_timezone(db))

    def serialize(self, row) -> dict:
        item = _row_to_dict(row)
        item.update(timing_fields(item, self.org_tz))
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
        item["is_legacy"] = item.get("report_type") in LEGACY_REPORT_TYPES
        item["replacement"] = LEGACY_REPLACEMENTS.get(item.get("report_type") or "")
        item["recipient_count"] = len(item.get("recipients") or []) + len(item.get("cc") or [])
        # Addresses are personal data: only report managers and the owner see
        # them (reports.view alone used to list every schedule's recipients).
        withheld = not (self.manager or item["is_owner"])
        item["recipients_withheld"] = withheld
        if withheld:
            item["recipients"], item["cc"] = [], []
            item["reply_to"] = None
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
    if await scoping.visible_tags(db, user) is not None:
        # An account limited to a visibility scope only sees its own schedules.
        rows = [r for r in rows if str(getattr(r, "created_by", "") or "") == str(user.id)]
    return {"data": [viewer.serialize(r) for r in rows]}


@router.get("/status")
async def report_schedules_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.view")),
):
    """Scheduler health (last tick, overdue schedules), whether report emails
    can carry a link, and the delivery settings."""
    from app.services import report_settings
    from app.services.report_scheduler import TICK_INTERVAL_S, scheduler_last_tick

    last_tick = await scheduler_last_tick(db)
    # A visibility-scoped account counts only the schedules it owns (the
    # ones it can list).
    own = await scoping.visible_tags(db, user) is not None
    overdue = (await db.execute(text(
        "SELECT count(*) FROM report_schedules WHERE enabled AND next_run_at < NOW() - make_interval(secs => :s) "
        "AND last_status IS DISTINCT FROM 'running'"
        + (" AND created_by = CAST(:me AS uuid)" if own else "")),
        {"s": OVERDUE_AFTER_S, **({"me": str(user.id)} if own else {})})).scalar() or 0
    healthy = last_tick is not None and \
        (datetime.now(timezone.utc) - last_tick).total_seconds() < TICK_INTERVAL_S * 5
    return {"scheduler_last_tick_at": last_tick.isoformat() if last_tick else None,
            "scheduler_healthy": healthy, "overdue_count": int(overdue),
            "share_links_available": await _share_links_available(db),
            "settings": await report_settings.load(db)}


@router.get("/{schedule_id}")
async def get_report_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.view")),
):
    row = await _load_detail(db, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    if await scoping.visible_tags(db, user) is not None and \
            str(getattr(row, "created_by", "") or "") != str(user.id):
        raise HTTPException(status_code=404, detail="Report schedule not found")
    return (await _Viewer.load(db, user)).serialize(row)


@router.post("", status_code=201)
async def create_report_schedule(
    data: ReportScheduleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    await _refuse_scoped(db, user)
    if data.report_type in LEGACY_REPORT_TYPES:
        raise HTTPException(400, f"The classic '{data.report_type}' report is retired: schedule the "
                                 f"'{LEGACY_REPLACEMENTS[data.report_type]}' template instead")
    _check_format_for_type(data.report_type, data.format)
    await _check_summary_only(db, data.format)
    timing = {"day_of_week": data.day_of_week, "day_of_month": data.day_of_month}
    _normalize_timing(timing, data.frequency)
    data.day_of_week, data.day_of_month = timing["day_of_week"], timing["day_of_month"]
    data.notify_channels = await _check_channels(db, data.notify_channels)
    _check_email_options({"group_send": data.group_send, "cc": data.cc, "recipients": data.recipients})
    if data.report_type == "custom":
        data.custom_report_id = await _readable_template_id(db, user, data.custom_report_id)
    else:
        data.custom_report_id = None
    data.filters = await _validated_filters(data.filters)
    tz_name = await get_configured_timezone(db)
    # The schedule keeps its own zone: the organisation's, unless one was chosen.
    data.timezone = data.timezone or tz_name
    sched = data.model_dump()
    next_run = compute_next_run(sched, tz_name) if data.enabled else None
    row = (await db.execute(
        text("""INSERT INTO report_schedules
                (name, description, enabled, report_type, period, format, filters,
                 frequency, hour, minute, day_of_week, day_of_month, timezone, notify_channels,
                 recipients, custom_report_id, next_run_at, created_by, created_at, updated_at,
                 skip_when_empty, subject_template, reply_to, group_send, cc)
                VALUES (:name, :description, :enabled, :report_type, :period, :format,
                        CAST(:filters AS jsonb), :frequency, :hour, :minute,
                        :day_of_week, :day_of_month, :timezone, CAST(:notify_channels AS jsonb),
                        CAST(:recipients AS jsonb), :custom_report_id, :next_run_at, :created_by, NOW(), NOW(),
                        :skip_when_empty, :subject_template, :reply_to, :group_send, CAST(:cc AS jsonb))
                RETURNING *"""),
        {
            "name": data.name, "description": data.description, "enabled": data.enabled,
            "report_type": data.report_type, "period": data.period, "format": data.format,
            "filters": json.dumps(data.filters or {}), "frequency": data.frequency,
            "custom_report_id": data.custom_report_id,
            "hour": data.hour, "minute": data.minute, "day_of_week": data.day_of_week,
            "day_of_month": data.day_of_month, "timezone": data.timezone,
            "notify_channels": json.dumps(data.notify_channels or []),
            "recipients": json.dumps(data.recipients or []),
            "next_run_at": next_run, "created_by": getattr(user, "id", None),
            "skip_when_empty": data.skip_when_empty, "subject_template": data.subject_template,
            "reply_to": data.reply_to, "group_send": data.group_send,
            "cc": json.dumps(data.cc or []),
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
    for key in ("report_type", "period", "format", "name", "enabled", "frequency", "hour", "minute",
                "skip_when_empty", "group_send"):
        if key in fields and fields[key] is None:
            raise HTTPException(status_code=400, detail=f"'{key}' cannot be empty")

    report_type = fields.get("report_type", current.report_type)
    if "report_type" in fields and report_type in LEGACY_REPORT_TYPES and report_type != current.report_type:
        raise HTTPException(400, f"The classic '{report_type}' report is retired: use the "
                                 f"'{LEGACY_REPLACEMENTS[report_type]}' template instead")
    if "report_type" in fields or "format" in fields:
        _check_format_for_type(report_type, fields.get("format", current.format))
    if "format" in fields and fields["format"] != current.format:
        await _check_summary_only(db, fields["format"])
    if {"frequency", "day_of_week", "day_of_month"} & set(fields):
        timing = {"day_of_week": fields.get("day_of_week", current.day_of_week),
                  "day_of_month": fields.get("day_of_month", current.day_of_month)}
        _normalize_timing(timing, fields.get("frequency", current.frequency))
        fields.update(timing)
    if "notify_channels" in fields:
        fields["notify_channels"] = await _check_channels(db, fields["notify_channels"] or [])
    if "cc" in fields and fields["cc"] is None:
        fields["cc"] = []
    _check_email_options(fields, current)
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
    tz_name = await get_configured_timezone(db)
    if "timezone" in fields and not fields["timezone"]:
        # Cleared: back to the organisation timezone, stored explicitly so a
        # later organisation change does not move the schedule.
        fields["timezone"] = tz_name

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
        elif key in ("notify_channels", "recipients", "cc"):
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
    timing_keys = {"enabled", "frequency", "hour", "minute", "day_of_week", "day_of_month", "timezone"}
    if timing_keys & set(fields.keys()):
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
    # Claim the schedule so a second click or the timer cannot run it in
    # parallel (the tick skips a schedule marked running in the last 30 min).
    claimed = (await db.execute(text(
        "UPDATE report_schedules SET last_status = 'running', updated_at = NOW() "
        "WHERE id = :id AND (last_status IS DISTINCT FROM 'running' OR updated_at IS NULL "
        "OR updated_at <= NOW() - INTERVAL '30 minutes') "
        "RETURNING id"), {"id": schedule_id})).first()
    await db.commit()
    if claimed is None:
        raise HTTPException(status_code=409, detail="This schedule is already running; wait for it to finish")
    sched = _row_to_dict(row)
    try:
        result = await generate_and_deliver(db, sched, triggered_by="manual")
    except Exception as exc:
        from app.services.report_scheduler import short_error
        await db.rollback()
        result = {"status": "failed", "errors": [short_error(exc)], "delivered": [], "run_id": None}
    await db.execute(
        text("UPDATE report_schedules SET last_run_at = NOW(), last_status = :st, last_error = :err, "
             "updated_at = NOW() WHERE id = :id"),
        {"st": result["status"], "err": "; ".join(result["errors"]) or None, "id": schedule_id},
    )
    await _audit(db, user, "run_now", row, status=result["status"], delivered=len(result["delivered"]))
    await db.commit()
    if result.get("status") == "skipped":
        message = "Report not sent: it has no data for this period (the schedule skips empty reports)"
    elif result.get("run_id"):
        message = f"Report generated ({result['status']})"
    else:
        message = f"Report not generated: {'; '.join(result['errors'])}"
    return {"message": message, **result}


@router.post("/{schedule_id}/convert")
async def convert_legacy_schedule(
    schedule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("reports.manage")),
):
    """Switch a schedule of a retired classic report type to the built-in
    template that replaces it; timing, delivery and scope are kept (a CSV
    attachment stays CSV, templates deliver CSV too)."""
    await _refuse_scoped(db, user)
    row = (await db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": schedule_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    target = LEGACY_REPLACEMENTS.get(row.report_type)
    if not target:
        raise HTTPException(status_code=400, detail="This schedule already delivers a report template")
    if target not in _presets():
        raise HTTPException(status_code=409, detail=f"The replacement template '{target}' is not available")
    fmt = row.format if row.format in TEMPLATE_FORMATS else "pdf"
    # Converting changes what is delivered: like any delivery edit by someone
    # other than the owner (and not a full admin), the editor takes it over.
    takeover = (str(getattr(row, "created_by", "") or "") != str(user.id)
                and not await _is_full_admin(db, user))
    await db.execute(text(
        "UPDATE report_schedules SET report_type = :rt, format = :fmt, custom_report_id = NULL, "
        + ("created_by = CAST(:owner AS uuid), " if takeover else "")
        + "updated_at = NOW() WHERE id = :id"),
        {"rt": target, "fmt": fmt, "id": schedule_id, **({"owner": str(user.id)} if takeover else {})})
    updated = await _load_detail(db, schedule_id)
    await _audit(db, user, "convert", updated, converted_from=row.report_type,
                 **({"owner_changed_from": str(row.created_by) if row.created_by else None} if takeover else {}))
    await db.commit()
    return (await _Viewer.load(db, user)).serialize(updated)


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
    if await scoping.visible_tags(db, user) is not None and \
            str(schedule.created_by or "") != str(user.id):
        raise HTTPException(status_code=404, detail="Report schedule not found")
    links = await sees_share_links(db, user, schedule.created_by)
    from app.api.v1.report_runs import RUN_COLUMNS, serialize_run
    rows = (await db.execute(
        text(f"""SELECT {RUN_COLUMNS} FROM report_runs r
                 LEFT JOIN report_schedules s ON s.id = r.schedule_id
                 WHERE r.schedule_id = :id ORDER BY r.generated_at DESC LIMIT 20"""),
        {"id": schedule_id},
    )).fetchall()
    manager = await is_report_manager(db, user)
    return {"data": [serialize_run(r, links=links, manager=manager) for r in rows],
            "can_view_links": links}
