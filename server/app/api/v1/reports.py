import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import has_permission
from app.core.security import get_current_user, get_role_permissions, require_operator_user, require_permission
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


@router.post("/generate")
async def generate_report_endpoint(
    data: ReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = REPORT_NAMES.get(data.report_type, 'Report')
    ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    # NetFlow alert messages are withheld from callers without netflow.view.
    redact = await nfa.netflow_details_withheld(db, current_user)

    try:
        common_args = dict(
            db=db,
            report_type=data.report_type,
            period=data.period,
            from_time=data.from_time,
            to_time=data.to_time,
            device_ids=data.device_ids,
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
    try:
        return await build_inventory(db)
    except Exception:
        logger.exception("inventory report data failed")
        raise HTTPException(status_code=500, detail="Inventory report data failed")


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
    current_user: User = Depends(require_operator_user),
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

from app.services import report_sections as _rs


def _window_from_query(
    from_: Optional[datetime], to: Optional[datetime], hours: int
) -> tuple[datetime, datetime]:
    end = to or datetime.utcnow()
    start = from_ or (end - timedelta(hours=max(1, min(hours, 24 * 92))))
    if end.tzinfo is not None:
        end = end.astimezone(tz=None).replace(tzinfo=None)
    if start.tzinfo is not None:
        start = start.astimezone(tz=None).replace(tzinfo=None)
    return start, end


@router.get("/catalog")
async def report_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everything the report library needs: built-in types (legacy + section
    presets), the section library for the custom builder, and saved custom
    reports."""
    legacy = [
        {"key": "executive_summary", "title": "Executive Summary",
         "description": "Leadership view: availability, SLA, incidents and MTTR.",
         "category": "Personas", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
        {"key": "device_health", "title": "Device Health",
         "description": "Per-device uptime, latency and status changes.",
         "category": "Personas", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
        {"key": "service_health", "title": "Service Health",
         "description": "Service checks, response times and TLS expiry.",
         "category": "Personas", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
        {"key": "alert_analysis", "title": "Alert Analysis",
         "description": "Alert volumes, severity mix and MTTR breakdown.",
         "category": "Personas", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
        {"key": "full_report", "title": "Full Report",
         "description": "All legacy sections in a single document.",
         "category": "Personas", "engine": "legacy", "formats": ["pdf", "excel", "csv"]},
    ]
    presets = [
        {"key": k, "title": p["title"], "description": p["description"],
         "category": p["category"], "engine": "sections",
         "formats": ["html", "pdf"], "sections": p["sections"],
         # Which scope filters this report's sections actually honour.
         "filterable": (["devices"] if k == "availability" else
                        ["applications"] if any(s.startswith("rum_") for s in p["sections"])
                        else ["netflow"] if any(s.startswith(("traffic_", "netflow_")) for s in p["sections"])
                        else [])}
        for k, p in _rs.REPORT_PRESETS.items()
    ]
    sections = [
        {"id": sid, "title": s["title"], "description": s["description"],
         "category": s["category"]}
        for sid, s in _rs.SECTION_REGISTRY.items()
    ]
    custom_rows = (await db.execute(text(
        "SELECT id::text, name, description, sections, created_at "
        "FROM custom_reports ORDER BY created_at DESC"))).fetchall()
    custom = [
        {"id": r[0], "name": r[1], "description": r[2] or "",
         "sections": list(r[3] or []), "created_at": r[4].isoformat() if r[4] else None}
        for r in custom_rows
    ]
    return {"types": legacy + presets, "sections": sections, "custom": custom}


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


@router.get("/render/{key}")
async def render_section_report(
    key: str,
    format: str = Query("json", pattern="^(json|html|pdf)$"),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
    hours: int = Query(24, ge=1, le=24 * 92),
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
    """Render a section-engine report (preset key, or key='custom' +
    custom_id) for an arbitrary window. NetFlow sections are restricted to
    the flow exporters the caller may see."""
    try:
        title, description, section_ids = await _rs.resolve_report(db, key, custom_id)
    except KeyError:
        raise HTTPException(404, f"Unknown report: {key}")

    filters: Optional[dict] = None
    if device_ids:
        ids = [i.strip() for i in device_ids.split(",") if i.strip()]
        try:
            for i in ids:
                _uuid.UUID(i)
        except ValueError:
            raise HTTPException(400, "device_ids must be UUIDs")
        if len(ids) > 500:
            raise HTTPException(400, "Too many device ids (max 500)")
        filters = {"device_ids": ids}
    if application_id:
        filters = {**(filters or {}), "application_id": application_id.strip()}
    if netflow:
        from app.api.v1.netflow.common import NetflowError
        from app.services import netflow_classify, netflow_groups, netflow_report
        await netflow_classify.load_rules()
        await netflow_groups.load_groups()
        try:
            nf_scope = netflow_report.scope_from_query(netflow)
        except NetflowError as exc:
            raise HTTPException(400, f"Invalid NetFlow scope: {exc.detail}")
        if nf_scope:
            filters = {**(filters or {}), "netflow": nf_scope}

    start, end = _window_from_query(from_, to, hours)
    sections = await _rs.build_sections(db, section_ids, start, end, filters, requester=current_user)
    category = (_rs.REPORT_PRESETS.get(key) or {}).get("category") or \
        ("Custom Report" if key == "custom" else "")
    scope_bits = []
    if filters and filters.get("device_ids"):
        scope_bits.append(f"{len(filters['device_ids'])} selected device(s)")
    if filters and filters.get("application_id"):
        scope_bits.append(f"application “{filters['application_id']}”")
    if filters and filters.get("netflow"):
        from app.services import netflow_report
        scope_bits.append(f"NetFlow: {netflow_report.describe(filters['netflow'])}")
    scope_label = " · ".join(scope_bits) if scope_bits else "All monitored infrastructure"
    meta = await _rs.build_report_meta(db, title, start, end,
                                      description=description,
                                      category=category,
                                      scope_label=scope_label)

    if format == "json":
        return {
            "key": key, "title": title, "description": description,
            "from": start.isoformat(), "to": end.isoformat(),
            "period_label": meta["period_label"],
            "generated_label": meta["generated_label"],
            "company_name": meta["company_name"],
            "scope_label": scope_label,
            "filters": {k: v for k, v in (filters or {}).items() if k in ("netflow", "application_id")},
            "sections": _rs.sections_to_json(sections),
        }

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe = title.replace(" ", "-")[:48]
    if format == "html":
        html = _rs.render_html(meta, sections)
        return Response(
            content=html, media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="ZenPlus-{safe}-{ts}.html"'},
        )
    pdf = _rs.render_pdf(meta, sections)
    return Response(
        content=bytes(pdf), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="ZenPlus-{safe}-{ts}.pdf"'},
    )


# ─── Custom reports CRUD ────────────────────────────────────────────────────

class CustomReportBody(BaseModel):
    name: str
    description: Optional[str] = None
    sections: list[str]


def _validate_custom(body: CustomReportBody) -> None:
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    if not body.sections:
        raise HTTPException(400, "Pick at least one section")
    unknown = [s for s in body.sections if s not in _rs.SECTION_REGISTRY]
    if unknown:
        raise HTTPException(400, f"Unknown sections: {unknown}")


@router.post("/custom", status_code=201)
async def create_custom_report(
    body: CustomReportBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_custom(body)
    import json as _json
    row = (await db.execute(text(
        "INSERT INTO custom_reports (name, description, sections, created_by) "
        "VALUES (:n, :d, CAST(:s AS jsonb), :u) RETURNING id::text"),
        {"n": body.name.strip(), "d": body.description,
         "s": _json.dumps(body.sections), "u": str(current_user.id)})).first()
    await db.commit()
    return {"id": row[0], "message": f"Custom report '{body.name}' saved"}


@router.put("/custom/{report_id}")
async def update_custom_report(
    report_id: _uuid.UUID,
    body: CustomReportBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_custom(body)
    import json as _json
    res = await db.execute(text(
        "UPDATE custom_reports SET name=:n, description=:d, "
        "sections=CAST(:s AS jsonb), updated_at=NOW() WHERE id=:id"),
        {"n": body.name.strip(), "d": body.description,
         "s": _json.dumps(body.sections), "id": str(report_id)})
    if not res.rowcount:
        raise HTTPException(404, "Custom report not found")
    await db.commit()
    return {"message": "Custom report updated"}


@router.delete("/custom/{report_id}")
async def delete_custom_report(
    report_id: _uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(text(
        "DELETE FROM custom_reports WHERE id=:id"), {"id": str(report_id)})
    if not res.rowcount:
        raise HTTPException(404, "Custom report not found")
    await db.commit()
    return {"message": "Custom report deleted"}


@router.get("/runs")
async def recent_report_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    """Recent generated reports across all schedules (for the library page).
    A run's share token only for its schedule's owner and reports.manage
    holders (a run whose schedule was deleted: reports.manage only)."""
    from app.api.v1.report_schedules import REPORTS_MANAGE_PERMISSION

    manager = has_permission(await get_role_permissions(db, current_user.role), REPORTS_MANAGE_PERMISSION)
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
