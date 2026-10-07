import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import has_permission
from app.core.security import get_current_user, get_role_permissions, require_permission
from app.models.user import User
from app.services import availability_policy
from app.services import netflow_alert_service as nfa
from app.services.audit_service import write_audit_log
from app.services.report_service import generate_report
from app.services.export_service import generate_excel_report, generate_csv_report
from app.services.report_data_service import (
    build_executive,
    build_technical,
    build_business,
    build_inventory,
)

router = APIRouter(prefix="/reports", tags=["Reports"])
logger = logging.getLogger(__name__)

REPORT_NAMES = {
    'executive_summary': 'Executive-Summary',
    'device_health': 'Device-Health',
    'service_health': 'Service-Health',
    'alert_analysis': 'Alert-Analysis',
    'full_report': 'Full-Report',
}


class ReportRequest(BaseModel):
    report_type: str = "executive_summary"
    period: str = "last_24h"
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    device_ids: Optional[list[str]] = None
    group_ids: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    device_types: Optional[list[str]] = None
    format: str = "pdf"  # pdf | excel | csv


# Legacy report types that read fleet-wide data the service layer cannot scope.
_LEGACY_UNSCOPABLE = {"service_health", "full_report"}


@router.post("/generate")
async def generate_report_endpoint(
    data: ReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.export")),
):
    name = REPORT_NAMES.get(data.report_type, 'Report')
    ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    # NetFlow alert messages are withheld from callers without netflow.view.
    redact = await nfa.netflow_details_withheld(db, current_user)

    # A tag-scoped account may only export what it can see: the report is
    # confined to its visible devices, and report types whose sections read
    # fleet-wide data are refused rather than leaked.
    scoped, device_ids = await scoping.scoped_device_filter(db, current_user, data.device_ids)
    if scoped:
        if data.report_type in _LEGACY_UNSCOPABLE:
            raise HTTPException(
                403,
                "This report includes fleet-wide sections and is unavailable to accounts "
                "limited to a visibility scope. Choose Device Health, Executive Summary or "
                "Alert Analysis instead.",
            )
        data = data.model_copy(update={"group_ids": None, "locations": None, "device_types": None})

    try:
        common_args = dict(
            db=db,
            report_type=data.report_type,
            period=data.period,
            from_time=data.from_time,
            to_time=data.to_time,
            device_ids=device_ids,
            group_ids=data.group_ids,
            locations=data.locations,
            device_types=data.device_types,
            redact_netflow=redact,
        )

        if data.format == "excel":
            content = await generate_excel_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.xlsx"
            return Response(
                content=content,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(content)),
                },
            )

        elif data.format == "csv":
            content = await generate_csv_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.csv"
            return Response(
                content=content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(content)),
                },
            )

        else:
            # Default: PDF
            pdf_bytes = await generate_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.pdf"
            return Response(
                content=bytes(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(pdf_bytes)),
                },
            )

    except Exception:
        logger.exception("report generation failed")
        raise HTTPException(status_code=500, detail="Report generation failed")


def _check_bounds(from_time: Optional[datetime], to_time: Optional[datetime]) -> None:
    def utc(ts: datetime) -> datetime:
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if from_time and to_time and utc(from_time) >= utc(to_time):
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")


@router.get("/data/executive")
async def get_executive_data(
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """KPIs, availability trend, top issues, location summary, outage timeline."""
    _check_bounds(from_time, to_time)
    scope = await scoping.visible_tags(db, current_user)
    try:
        return await build_executive(db, from_time, to_time, visible_tags=scope)
    except Exception:
        logger.exception("executive report data failed")
        raise HTTPException(status_code=500, detail="Executive report data failed")


@router.get("/data/technical")
async def get_technical_data(
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Top-N worst devices, noisy alerts, bandwidth interfaces, alert volume.
    NetFlow alert messages are withheld from callers without netflow.view."""
    _check_bounds(from_time, to_time)
    scope = await scoping.visible_tags(db, current_user)
    redact = await nfa.netflow_details_withheld(db, current_user)
    try:
        return await build_technical(db, from_time, to_time, visible_tags=scope, redact_netflow=redact)
    except Exception:
        logger.exception("technical report data failed")
        raise HTTPException(status_code=500, detail="Technical report data failed")


@router.get("/data/business")
async def get_business_data(
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Service availability, response time quantiles, TLS warnings, customer impact."""
    _check_bounds(from_time, to_time)
    scope = await scoping.visible_tags(db, current_user)
    try:
        return await build_business(db, from_time, to_time, visible_tags=scope)
    except Exception:
        logger.exception("business report data failed")
        raise HTTPException(status_code=500, detail="Business report data failed")


@router.get("/data/inventory")
async def get_inventory_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Devices by type/vendor/location, interfaces totals, sensor fleet, recently added."""
    if await scoping.visible_tags(db, current_user) is not None:
        raise HTTPException(403, "The fleet inventory report is unavailable to accounts limited to a visibility scope")
    try:
        return await build_inventory(db)
    except Exception:
        logger.exception("inventory report data failed")
        raise HTTPException(status_code=500, detail="Inventory report data failed")


@router.get("/branding")
async def get_report_branding(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Organisation branding and regional formats for on-screen reports.

    Report viewers need the logo, colour, timezone and date/time format
    without holding settings.view (module default: reports.view)."""
    from app.services.branding import load_branding
    return (await load_branding(db)).public_dict()


class AvailabilityPolicyBody(BaseModel):
    default_target_pct: float = Field(..., ge=availability_policy.MIN_TARGET_PCT, le=100)


@router.get("/availability-policy")
async def get_availability_policy(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Default availability target applied to services without their own."""
    target, source = await availability_policy.load_default_target(db)
    return {"default_target_pct": target, "source": source}


@router.put("/availability-policy")
async def update_availability_policy(
    body: AvailabilityPolicyBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    previous, _source = await availability_policy.load_default_target(db)
    await availability_policy.save_default_target(db, body.default_target_pct)
    await write_audit_log(
        db, actor=current_user, action="reports.availability_policy.update",
        resource_type="system_settings", resource_id=availability_policy.SETTINGS_KEY,
        metadata={"default_target_pct": body.default_target_pct, "previous": previous},
    )
    await db.commit()
    return {"default_target_pct": body.default_target_pct, "source": "policy"}


# The token is in the URL: never send it on as a Referer, never cache or index the page.
SHARED_REPORT_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    # The stored page is self-contained (inline styles, data: images, no
    # scripts): nothing else may load or run when a recipient opens it.
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/shared/{token}", response_class=HTMLResponse)
async def view_shared_report(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Token-gated, read-only HTML view of a generated report.

    Intentionally unauthenticated so email recipients (who are not logged in)
    can open the "View full report" link. The 32+ char random token is the
    capability; rows can carry an optional expires_at. The page is what the
    schedule's owner may see, so the token is only ever listed to that owner
    and reports.manage holders (``report_schedules.sees_share_links``), and
    the response is kept out of caches, referrers and search indexes.
    """
    row = (await db.execute(
        text("SELECT html, expires_at FROM report_runs WHERE token = :t"),
        {"t": token},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found or expired")
    if row.expires_at is not None:
        from datetime import datetime, timezone
        if row.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="This report link has expired")
    return HTMLResponse(content=row.html, headers=SHARED_REPORT_HEADERS)



@router.get("/types")
async def list_report_types(
    current_user: User = Depends(get_current_user),
):
    return {
        "report_types": [
            {
                "id": "executive_summary",
                "name": "Executive Summary",
                "description": "High-level overview with KPIs, health scores, and charts for management",
                "icon": "BarChart3",
            },
            {
                "id": "device_health",
                "name": "Device Health Report",
                "description": "Detailed per-device uptime, RTT analysis, and status history",
                "icon": "Monitor",
            },
            {
                "id": "service_health",
                "name": "Service Health Report",
                "description": "HTTP, TCP, and TLS service check analysis with response times",
                "icon": "ShieldCheck",
            },
            {
                "id": "alert_analysis",
                "name": "Alert Analysis",
                "description": "Alert trends, severity breakdown, MTTR, and top alerting devices",
                "icon": "Bell",
            },
            {
                "id": "full_report",
                "name": "Full Comprehensive Report",
                "description": "Complete report combining all sections — devices, services, and alerts",
                "icon": "FileText",
            },
        ],
        "periods": [
            {"id": "last_24h", "label": "Last 24 Hours"},
            {"id": "last_7d", "label": "Last 7 Days"},
            {"id": "last_30d", "label": "Last 30 Days"},
            {"id": "custom", "label": "Custom Range"},
        ],
    }


# ─── Section-engine reports (availability/performance/traffic/usage/…) ──────
#
# The section-based engine (services/report_sections.py) serves the newer
# report types and user-defined custom reports in three formats: JSON for the
# dashboard's generic viewer, self-contained HTML, and PDF.

import uuid as _uuid

from app.services import report_render as _render
from app.services import report_sections as _rs


def _window_from_query(
    from_: Optional[datetime], to: Optional[datetime], hours: int
) -> tuple[datetime, datetime]:
    # Naive UTC throughout (the section engine's convention). Aware inputs are
    # converted to UTC — astimezone() with no argument would use the server
    # OS zone and silently shift the window.
    if to is not None and to.tzinfo is not None:
        to = to.astimezone(timezone.utc).replace(tzinfo=None)
    if from_ is not None and from_.tzinfo is not None:
        from_ = from_.astimezone(timezone.utc).replace(tzinfo=None)
    end = to or datetime.now(timezone.utc).replace(tzinfo=None)
    start = from_ or (end - timedelta(hours=max(1, min(hours, 24 * 92))))
    if start >= end:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")
    if end - start > timedelta(days=400):
        raise HTTPException(status_code=400, detail="Report window is limited to 400 days")
    return start, end


LEGACY_TYPES = [
    {"key": "executive_summary", "title": "Executive Summary",
     "description": "Leadership view: availability, SLA, incidents and MTTR.",
     "category": "legacy", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
    {"key": "device_health", "title": "Device Health",
     "description": "Per-device uptime, latency and status changes.",
     "category": "legacy", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
    {"key": "service_health", "title": "Service Health",
     "description": "Service checks, response times and TLS expiry.",
     "category": "legacy", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
    {"key": "alert_analysis", "title": "Alert Analysis",
     "description": "Alert volumes, severity mix and MTTR breakdown.",
     "category": "legacy", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
    {"key": "full_report", "title": "Full Report",
     "description": "All legacy sections in a single document.",
     "category": "legacy", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
]

PERIOD_LABELS = [
    {"id": "last_24h", "label": "Last 24 hours", "kind": "rolling"},
    {"id": "last_7d", "label": "Last 7 days", "kind": "rolling"},
    {"id": "last_30d", "label": "Last 30 days", "kind": "rolling"},
    {"id": "last_90d", "label": "Last 90 days", "kind": "rolling"},
    {"id": "today", "label": "Today", "kind": "calendar"},
    {"id": "yesterday", "label": "Yesterday", "kind": "calendar"},
    {"id": "week_to_date", "label": "This week", "kind": "calendar"},
    {"id": "previous_week", "label": "Previous week", "kind": "calendar"},
    {"id": "month_to_date", "label": "This month", "kind": "calendar"},
    {"id": "previous_month", "label": "Previous month", "kind": "calendar"},
    {"id": "quarter_to_date", "label": "This quarter", "kind": "calendar"},
    {"id": "previous_quarter", "label": "Previous quarter", "kind": "calendar"},
]


async def _access(db: AsyncSession, user: User) -> tuple[Optional[frozenset], bool, bool]:
    """(section permissions, is full admin, is report manager) for the caller."""
    from app.api.v1.report_schedules import is_report_manager
    from app.core.permissions import SUPERUSER_PERMISSION
    perms = await _rs.requester_permissions(db, user)
    admin = perms is None or SUPERUSER_PERMISSION in perms
    return perms, admin, await is_report_manager(db, user)


@router.get("/catalog")
async def report_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everything the library, viewer and designer need: categories, built-in
    templates (each marked with whether the caller can see its sections), the
    section library with option schemas, the caller's readable organisation
    templates, the period vocabulary and the caller's report capabilities."""
    from app.core.permissions import has_permission
    from app.services import report_templates as _rt

    perms, admin, manager = await _access(db, current_user)
    templates = [_rt.catalog_entry(t, perms) for t in _rt.TEMPLATES.values()]
    sections = [
        {"id": sid, "title": s["title"], "description": s["description"],
         "category": s["category"], "scope": s["scope"], "requires": list(s["requires"]),
         "options": s["options"], "semantics": s["semantics"],
         "available": _rs.may_view_section(perms, sid)}
        for sid, s in _rs.SECTION_REGISTRY.items()
    ]
    rows = (await db.execute(text(
        f"SELECT {_render.TEMPLATE_COLUMNS} FROM custom_reports c "
        "LEFT JOIN users u ON u.id = c.created_by "
        "WHERE c.visibility = 'shared' OR c.created_by = :me OR :admin "
        "ORDER BY c.updated_at DESC NULLS LAST, c.created_at DESC"),
        {"me": str(current_user.id), "admin": admin})).fetchall()
    custom = [_render.serialize_template(r, current_user, admin, manager) for r in rows]
    granted = perms if perms is not None else frozenset({"system.admin"})
    return {
        "categories": _rt.CATEGORIES,
        "types": LEGACY_TYPES + templates,
        "sections": sections,
        "custom": custom,
        "periods": PERIOD_LABELS,
        "can": {
            "manage": admin or has_permission(granted, "reports.manage"),
            "export": admin or has_permission(granted, "reports.export"),
            "share": admin or manager,
        },
    }


@router.get("/rum-applications")
async def list_rum_applications(
    days: int = Query(30, ge=1, le=90),
    current_user: User = Depends(get_current_user),
):
    """Applications that have reported browser telemetry, for the report scope picker."""
    from app.core.database import get_clickhouse_client
    out: list[dict] = []
    try:
        rows = get_clickhouse_client().query(
            """
            SELECT application_id, anyHeavy(service_name), uniq(session_id) AS s, max(timestamp)
            FROM zenplus.apm_rum_events
            WHERE timestamp >= now() - INTERVAL %(d)s DAY AND application_id != ''
            GROUP BY application_id ORDER BY s DESC LIMIT 200
            """,
            parameters={"d": days},
        ).result_rows
        for r in rows:
            out.append({
                "application_id": str(r[0]),
                "service_name": str(r[1] or ""),
                "sessions": int(r[2] or 0),
                "last_seen": r[3].isoformat() if r[3] else None,
            })
    except Exception:
        logger.debug("rum applications unavailable", exc_info=True)
    return {"days": days, "applications": out}


async def _require_export(db: AsyncSession, user: User) -> None:
    from app.core.permissions import has_permission
    perms = await get_role_permissions(db, user.role)
    if not has_permission(perms, "reports.export"):
        raise HTTPException(403, "Downloading reports requires the reports.export permission")


async def _resolve_for(db: AsyncSession, user: User, key: str,
                       custom_id: Optional[str]) -> tuple[str, str, str, list]:
    """(title, description, category label, entries) for a built-in key or a
    readable organisation template. Unreadable templates are 404."""
    if key == "custom":
        _perms, admin, _manager = await _access(db, user)
        row = await _render.load_template(db, custom_id)
        if not _render.can_read_template(row, user, admin):
            raise HTTPException(404, "Report template not found")
        from app.services import report_templates as _rt
        category = _rt.CATEGORY_TITLES.get(row.category or "", "") or "Custom report"
        return row.name, row.description or "", category, _render.template_entries(row)
    preset = _rs.REPORT_PRESETS.get(key)
    if not preset:
        raise HTTPException(404, f"Unknown report: {key}")
    entries = preset.get("entries") or preset["sections"]
    return preset["title"], preset["description"], preset.get("category") or "", entries


def _file_response(out) -> Response:
    body, media, disposition = out
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store"})


@router.get("/render/{key}")
async def render_section_report(
    key: str,
    format: str = Query("json", pattern="^(json|html|pdf|xlsx)$"),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
    hours: int = Query(24, ge=1, le=24 * 400),
    custom_id: Optional[str] = None,
    device_ids: Optional[str] = Query(None, description="Comma-separated device UUIDs to scope node-aware sections"),
    application_id: Optional[str] = Query(None, max_length=128,
                                          description="Scope browser RUM sections to one application; omit for all"),
    netflow: Optional[str] = Query(None, max_length=2000,
                                   description="NetFlow scope for traffic sections: a URL-encoded flow filter "
                                               "query string, e.g. exporter=10.0.0.1&app=DNS"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render a built-in template (key) or an organisation template
    (key='custom' + custom_id) for a window: JSON for the viewer, or a branded
    PDF / HTML / Excel download (downloads need reports.export). Sections the
    caller's role cannot see are replaced by a 'restricted' stub; NetFlow
    sections are restricted to the exporters the caller may see."""
    if format != "json":
        await _require_export(db, current_user)
    title, description, category, entries = await _resolve_for(db, current_user, key, custom_id)
    filters = await _render.build_filters(_render.parse_device_ids(device_ids), application_id, netflow)
    start, end = _window_from_query(from_, to, hours)
    out = await _render.render(db, user=current_user, key=key, title=title, description=description,
                               category=category, entries=entries, start=start, end=end,
                               filters=filters, fmt=format)
    return out if format == "json" else _file_response(out)


class TemplateSection(BaseModel):
    id: str = Field(..., max_length=64)
    title: Optional[str] = Field(None, max_length=120)
    options: dict = Field(default_factory=dict)


class PreviewBody(BaseModel):
    name: str = Field("Untitled report", max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    category: Optional[str] = Field(None, max_length=60)
    sections: list[Union[str, TemplateSection]] = Field(..., min_length=1, max_length=_render.MAX_SECTIONS)
    from_: Optional[datetime] = Field(None, alias="from")
    to: Optional[datetime] = None
    hours: int = Field(24, ge=1, le=24 * 400)
    device_ids: Optional[list[str]] = None
    application_id: Optional[str] = Field(None, max_length=128)
    netflow: Optional[str] = Field(None, max_length=2000)

    model_config = {"populate_by_name": True}


def _entry_dicts(items: list) -> list[dict]:
    return [i if isinstance(i, str) else i.model_dump() for i in items]


def _validate_entries(items: list) -> list[dict]:
    entries = _rs.section_entries(_entry_dicts(items))
    if not entries:
        raise HTTPException(400, "Pick at least one section")
    if len(entries) > _render.MAX_SECTIONS:
        raise HTTPException(400, f"A report can hold at most {_render.MAX_SECTIONS} sections")
    errors: list[str] = []
    for e in entries:
        errors.extend(_rs.option_errors(e["id"], e["options"]))
    if errors:
        raise HTTPException(400, "; ".join(errors[:8]))
    return entries


@router.post("/preview")
async def preview_report(
    body: PreviewBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    """Render an unsaved template definition (the designer's live preview)."""
    entries = _validate_entries(body.sections)
    from app.services import report_templates as _rt
    filters = await _render.build_filters(_render.parse_device_ids(body.device_ids),
                                          body.application_id, body.netflow)
    start, end = _window_from_query(body.from_, body.to, body.hours)
    return await _render.render(
        db, user=current_user, key="preview", title=body.name.strip() or "Untitled report",
        description=body.description or "", category=_rt.CATEGORY_TITLES.get(body.category or "", ""),
        entries=entries, start=start, end=end, filters=filters, fmt="json")


# ─── Organisation templates (custom_reports) ────────────────────────────────

class CustomReportBody(BaseModel):
    name: str = Field(..., max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    sections: list[Union[str, TemplateSection]] = Field(..., max_length=_render.MAX_SECTIONS)
    category: Optional[str] = Field(None, max_length=60)
    tags: list[str] = Field(default_factory=list, max_length=12)
    visibility: str = Field("shared", pattern="^(private|shared)$")
    source_key: Optional[str] = Field(None, max_length=80)
    default_period: Optional[str] = Field(None, max_length=24)


def _validate_custom(body: CustomReportBody) -> list[dict]:
    from app.services import report_templates as _rt
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    unknown = [e for e in _rs.section_entries(_entry_dicts(body.sections)) if e["id"] not in _rs.SECTION_REGISTRY]
    if unknown:
        raise HTTPException(400, f"Unknown sections: {[e['id'] for e in unknown]}")
    if body.category and body.category not in _rt.CATEGORY_TITLES:
        raise HTTPException(400, "Unknown category")
    if body.default_period and body.default_period not in _rt.PERIODS:
        raise HTTPException(400, "Unknown default period")
    if body.source_key and body.source_key not in _rt.TEMPLATES:
        raise HTTPException(400, "Unknown source template")
    for t in body.tags:
        if not t.strip() or len(t) > 32:
            raise HTTPException(400, "Tags are 1-32 characters")
    return _validate_entries(body.sections)


def _refuse_unviewable(perms, entries: list[dict], already: frozenset = frozenset()) -> None:
    """A template author may only add sections they are allowed to see: a
    shared template is delivered by other people's schedules, which render
    with their owners' permissions."""
    bad = sorted({e["id"] for e in entries
                  if e["id"] not in already and not _rs.may_view_section(perms, e["id"])})
    if bad:
        titles = [(_rs.SECTION_REGISTRY.get(b) or {}).get("title", b) for b in bad]
        raise HTTPException(403, "You can't add sections you don't have permission to view: "
                                 + ", ".join(titles))


def _template_params(body: CustomReportBody, entries: list[dict]) -> dict:
    import json as _json
    return {
        "n": body.name.strip(), "d": (body.description or "").strip() or None,
        "s": _json.dumps([e["id"] for e in entries]),
        "def": _json.dumps({"version": 1, "sections": entries}),
        "cat": body.category or None,
        "tags": _json.dumps(sorted({t.strip() for t in body.tags if t.strip()})),
        "vis": body.visibility, "src": body.source_key or None,
        "per": body.default_period or None,
    }


@router.get("/custom/{report_id}")
async def get_custom_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _perms, admin, manager = await _access(db, current_user)
    row = await _render.load_template(db, report_id)
    if not _render.can_read_template(row, current_user, admin):
        raise HTTPException(404, "Report template not found")
    return _render.serialize_template(row, current_user, admin, manager)


@router.post("/custom", status_code=201)
async def create_custom_report(
    body: CustomReportBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    entries = _validate_custom(body)
    perms, _admin, _manager = await _access(db, current_user)
    _refuse_unviewable(perms, entries)
    params = _template_params(body, entries)
    row = (await db.execute(text(
        "INSERT INTO custom_reports (name, description, sections, definition, category, tags, "
        "visibility, source_key, default_period, created_by, updated_by) "
        "VALUES (:n, :d, CAST(:s AS jsonb), CAST(:def AS jsonb), :cat, CAST(:tags AS jsonb), "
        ":vis, :src, :per, :u, :u) RETURNING id::text"),
        {**params, "u": str(current_user.id)})).first()
    await write_audit_log(
        db, actor=current_user, action="reports.template.create", resource_type="report_template",
        resource_id=row[0], metadata={"name": params["n"], "visibility": body.visibility,
                                      "source_key": body.source_key,
                                      "sections": [e["id"] for e in entries]})
    await db.commit()
    return {"id": row[0], "message": f"Report template '{params['n']}' saved"}


@router.put("/custom/{report_id}")
async def update_custom_report(
    report_id: str,
    body: CustomReportBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    perms, admin, manager = await _access(db, current_user)
    row = await _render.load_template(db, report_id)
    if not _render.can_read_template(row, current_user, admin):
        raise HTTPException(404, "Report template not found")
    if not _render.can_edit_template(row, current_user, admin, manager):
        raise HTTPException(403, "Only the owner or a report manager can change this template")
    if body.visibility != row.visibility and not (admin or _render.is_owner(row, current_user)):
        raise HTTPException(403, "Only the owner can change who sees this template")
    entries = _validate_custom(body)
    _refuse_unviewable(perms, entries,
                       already=frozenset(e["id"] for e in _render.template_entries(row)))
    params = _template_params(body, entries)
    await db.execute(text(
        "UPDATE custom_reports SET name=:n, description=:d, sections=CAST(:s AS jsonb), "
        "definition=CAST(:def AS jsonb), category=:cat, tags=CAST(:tags AS jsonb), visibility=:vis, "
        "source_key=:src, default_period=:per, updated_by=:u, updated_at=NOW() WHERE id=:id"),
        {**params, "u": str(current_user.id), "id": row.id})
    changed = [k for k, old in (("name", row.name), ("visibility", row.visibility),
                                ("category", row.category), ("default_period", row.default_period))
               if (getattr(body, k) or None) != (old or None)]
    await write_audit_log(
        db, actor=current_user, action="reports.template.update", resource_type="report_template",
        resource_id=row.id, metadata={"name": params["n"], "changed": changed,
                                      "sections": [e["id"] for e in entries]})
    await db.commit()
    return {"message": "Report template updated"}


@router.post("/custom/{report_id}/duplicate", status_code=201)
async def duplicate_custom_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    """A private copy owned by the caller."""
    import json as _json
    _perms, admin, _manager = await _access(db, current_user)
    row = await _render.load_template(db, report_id)
    if not _render.can_read_template(row, current_user, admin):
        raise HTTPException(404, "Report template not found")
    entries = _render.template_entries(row)
    name = f"Copy of {row.name}"[:120]
    new = (await db.execute(text(
        "INSERT INTO custom_reports (name, description, sections, definition, category, tags, "
        "visibility, source_key, default_period, created_by, updated_by) "
        "VALUES (:n, :d, CAST(:s AS jsonb), CAST(:def AS jsonb), :cat, CAST(:tags AS jsonb), "
        "'private', :src, :per, :u, :u) RETURNING id::text"),
        {"n": name, "d": row.description, "s": _json.dumps([e["id"] for e in entries]),
         "def": _json.dumps({"version": 1, "sections": entries}), "cat": row.category,
         "tags": _json.dumps(list(row.tags or [])), "src": row.source_key, "per": row.default_period,
         "u": str(current_user.id)})).first()
    await write_audit_log(
        db, actor=current_user, action="reports.template.clone", resource_type="report_template",
        resource_id=new[0], metadata={"name": name, "source_id": row.id})
    await db.commit()
    return {"id": new[0], "message": f"Created '{name}'"}


@router.delete("/custom/{report_id}")
async def delete_custom_report(
    report_id: str,
    force: bool = Query(False, description="Also delete the schedules that deliver this template"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    _perms, admin, manager = await _access(db, current_user)
    row = await _render.load_template(db, report_id)
    if not _render.can_read_template(row, current_user, admin):
        raise HTTPException(404, "Report template not found")
    if not _render.can_edit_template(row, current_user, admin, manager):
        raise HTTPException(403, "Only the owner or a report manager can delete this template")
    if int(row.schedules or 0) and not force:
        raise HTTPException(409, f"{row.schedules} schedule(s) deliver this template. Delete them too, "
                                 "or keep the template.")
    if int(row.schedules or 0):
        # The schedules go with the template: only someone who may delete each
        # of them (a report manager, or their owner) may force this, and every
        # removed schedule is audited like a schedule delete.
        dependants = (await db.execute(text(
            "SELECT id, name, report_type, period, format, created_by FROM report_schedules "
            "WHERE custom_report_id = :id"), {"id": row.id})).fetchall()
        if not (admin or manager) and any(str(d.created_by or "") != str(current_user.id) for d in dependants):
            raise HTTPException(403, "Other people's schedules deliver this template; ask a report "
                                     "manager to remove them first.")
        from app.api.v1.report_schedules import _refuse_scoped
        await _refuse_scoped(db, current_user)
        for d in dependants:
            await db.execute(text("DELETE FROM report_schedules WHERE id = :id"), {"id": d.id})
            await write_audit_log(
                db, actor=current_user, action="reports.schedule.delete", resource_type="report_schedule",
                resource_id=str(d.id), metadata={"name": d.name, "report_type": d.report_type,
                                                 "reason": "template deleted", "template_id": row.id})
    await db.execute(text("DELETE FROM custom_reports WHERE id=:id"), {"id": row.id})
    await write_audit_log(
        db, actor=current_user, action="reports.template.delete", resource_type="report_template",
        resource_id=row.id, metadata={"name": row.name, "schedules_removed": int(row.schedules or 0)})
    await db.commit()
    return {"message": "Report template deleted"}


@router.get("/runs")
async def recent_report_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    """Recent generated reports across all schedules (for the library page).
    A run's share token only for its schedule's owner and report managers
    (reports.manage holders not limited to a visibility scope; a run whose
    schedule was deleted: report managers only)."""
    from app.api.v1.report_schedules import is_report_manager

    manager = await is_report_manager(db, current_user)
    me = str(current_user.id)
    rows = (await db.execute(text(
        """SELECT r.id::text, r.title, r.report_type, r.period, r.token,
                  r.status, r.delivered_to, r.generated_at, s.name AS schedule_name,
                  s.created_by::text AS owner_id
           FROM report_runs r
           LEFT JOIN report_schedules s ON s.id = r.schedule_id
           ORDER BY r.generated_at DESC LIMIT :lim"""), {"lim": limit})).fetchall()
    out = []
    for r in rows:
        links = manager or (r[9] is not None and r[9] == me)
        out.append({"id": r[0], "title": r[1], "report_type": r[2], "period": r[3],
                    "token": r[4] if links else None, "token_withheld": not links,
                    "status": r[5], "delivered_to": r[6] or [],
                    "generated_at": r[7].isoformat() if r[7] else None,
                    "schedule_name": r[8]})
    return out
