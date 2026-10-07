"""One render pipeline for section-engine reports.

Used by ``GET /reports/render/{key}`` (viewer and downloads), ``POST
/reports/preview`` (the template designer) and the scheduler, so a report
looks the same wherever it is produced: same scoping rules, same branding,
same file names.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.services import report_sections as rs
from app.services.branding import Branding, load_branding

MAX_DEVICE_FILTER = 500
MAX_SECTIONS = 40

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# ─── File names ─────────────────────────────────────────────────────────────

def _slug(value: str, limit: int = 48) -> str:
    ascii_ = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    ascii_ = re.sub(r"[^A-Za-z0-9]+", "-", ascii_).strip("-")
    return ascii_[:limit].strip("-")


def report_filename(title: str, ext: str, branding: Optional[Branding] = None,
                    at: Optional[datetime] = None) -> str:
    """'<Company>-<Title>-<YYYYMMDD-HHMM>.<ext>' in the organisation timezone."""
    b = branding or Branding()
    local = b.localize(at or datetime.now(timezone.utc))
    company = _slug(b.display_name, 32) or "ZenPlus"
    title_slug = _slug(title) or "Report"
    return f"{company}-{title_slug}-{local:%Y%m%d-%H%M}.{ext}"


def content_disposition(title: str, ext: str, branding: Optional[Branding] = None) -> str:
    """ASCII filename plus an RFC 5987 UTF-8 filename* (titles may be non-Latin)."""
    ascii_name = report_filename(title, ext, branding)
    b = branding or Branding()
    local = b.localize(datetime.now(timezone.utc))
    utf8 = f"{b.display_name}-{(title or 'Report').strip()}-{local:%Y%m%d-%H%M}.{ext}"
    utf8 = re.sub(r'[\\/:*?"<>|\r\n]+', "-", utf8)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(utf8)}"


# ─── Scope filters ──────────────────────────────────────────────────────────

def parse_device_ids(raw: Any) -> Optional[list[str]]:
    if raw is None or raw == "" or raw == []:
        return None
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    ids = [str(i).strip() for i in items if str(i).strip()]
    if len(ids) > MAX_DEVICE_FILTER:
        raise HTTPException(400, f"Too many device ids (max {MAX_DEVICE_FILTER})")
    for i in ids:
        try:
            _uuid.UUID(i)
        except ValueError:
            raise HTTPException(400, "device_ids must be UUIDs")
    return ids or None


async def build_filters(device_ids: Optional[list[str]], application_id: Optional[str],
                        netflow: Optional[str]) -> Optional[dict]:
    filters: dict[str, Any] = {}
    if device_ids:
        filters["device_ids"] = device_ids
    if application_id:
        filters["application_id"] = application_id.strip()[:128]
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
            filters["netflow"] = nf_scope
    return filters or None


def scope_label_for(filters: Optional[dict]) -> str:
    bits = []
    if filters and filters.get("device_ids"):
        bits.append(f"{len(filters['device_ids'])} selected device(s)")
    if filters and filters.get("application_id"):
        bits.append(f"application “{filters['application_id']}”")
    if filters and filters.get("netflow"):
        from app.services import netflow_report
        bits.append(f"NetFlow: {netflow_report.describe(filters['netflow'])}")
    return " · ".join(bits) if bits else "All monitored infrastructure"


# ─── Render ─────────────────────────────────────────────────────────────────

async def render(db: AsyncSession, *, user: Any, key: str, title: str, description: str,
                 category: str, entries: list, start: datetime, end: datetime,
                 filters: Optional[dict], fmt: str = "json") -> Any:
    """Render a report for ``user`` (the viewer). Returns the JSON dict, or
    ``(bytes, media_type, content_disposition)`` for file formats.

    Accounts limited to a visibility scope only run scope-safe sections, with
    the device filter pinned to what they can see (fail closed)."""
    entries = rs.section_entries(entries)[:MAX_SECTIONS]
    omitted = 0
    scoped, scoped_ids = await scoping.scoped_device_filter(db, user, (filters or {}).get("device_ids"))
    if scoped:
        kept = [e for e in entries if e["id"] in rs.SCOPE_SAFE_SECTIONS]
        omitted = len(entries) - len(kept)
        if not kept:
            raise HTTPException(403, "This report contains only fleet-wide sections, which are unavailable "
                                     "to accounts limited to a visibility scope")
        entries = kept
        filters = {**(filters or {}), "device_ids": scoped_ids}
        filters.pop("application_id", None)

    branding = await load_branding(db)
    sections = await rs.build_sections(db, entries, start, end, filters, requester=user, branding=branding)

    scope_label = scope_label_for(filters)
    if scoped:
        scope_label = "Limited to your visibility scope" + (
            f" ({omitted} fleet-wide section(s) omitted)" if omitted else "")
    meta = await rs.build_report_meta(db, title, start, end, description=description, category=category,
                                      scope_label=scope_label, branding=branding)

    if fmt == "json":
        return {
            "key": key, "title": title, "description": description, "category": category,
            "from": start.replace(tzinfo=timezone.utc).isoformat() if start.tzinfo is None else start.isoformat(),
            "to": end.replace(tzinfo=timezone.utc).isoformat() if end.tzinfo is None else end.isoformat(),
            "period_label": meta["period_label"],
            "generated_label": meta["generated_label"],
            "company_name": meta["company_name"],
            "timezone": branding.timezone,
            "tz_label": branding.tz_label(end),
            "scope_label": scope_label,
            "scoped": scoped,
            "omitted_sections": omitted,
            "restricted_sections": sum(1 for s in sections if s.get("restricted")),
            "filters": {k: v for k, v in (filters or {}).items() if k in ("netflow", "application_id")},
            "branding": branding.public_dict(),
            "sections": rs.sections_to_json(sections),
        }
    # Rendering is CPU-bound (matplotlib, fpdf2, openpyxl): keep it off the
    # event loop so one large report does not stall the API worker.
    if fmt == "html":
        body: bytes = (await asyncio.to_thread(rs.render_html, meta, sections)).encode("utf-8")
    elif fmt == "pdf":
        body = bytes(await asyncio.to_thread(rs.render_pdf, meta, sections))
    elif fmt == "xlsx":
        from app.services.report_xlsx import render_xlsx
        body = await asyncio.to_thread(render_xlsx, meta, sections)
    else:
        raise HTTPException(400, "Unknown format")
    return body, MEDIA_TYPES[fmt], content_disposition(title, fmt, branding)


# ─── Organisation templates (custom_reports) ────────────────────────────────

TEMPLATE_COLUMNS = (
    "c.id::text AS id, c.name, c.description, c.sections, c.definition, c.category, c.tags, "
    "c.visibility, c.source_key, c.default_period, c.created_by::text AS owner_id, "
    "COALESCE(NULLIF(u.full_name, ''), u.username) AS owner_name, c.created_at, c.updated_at, "
    "(SELECT count(*) FROM report_schedules s WHERE s.custom_report_id = c.id) AS schedules"
)


def template_entries(row: Any) -> list[dict]:
    definition = getattr(row, "definition", None) or {}
    items = definition.get("sections") if isinstance(definition, dict) else None
    return rs.section_entries(items or list(getattr(row, "sections", None) or []))


async def load_template(db: AsyncSession, template_id: Any) -> Any:
    try:
        tid = str(_uuid.UUID(str(template_id)))
    except (TypeError, ValueError):
        return None
    return (await db.execute(text(
        f"SELECT {TEMPLATE_COLUMNS} FROM custom_reports c "
        "LEFT JOIN users u ON u.id = c.created_by WHERE c.id = :id"), {"id": tid})).first()


def is_owner(row: Any, user: Any) -> bool:
    return row is not None and row.owner_id is not None and str(row.owner_id) == str(getattr(user, "id", ""))


def can_read_template(row: Any, user: Any, is_admin: bool) -> bool:
    return row is not None and (is_admin or row.visibility == "shared" or is_owner(row, user))


def can_edit_template(row: Any, user: Any, is_admin: bool, is_manager: bool) -> bool:
    """Owners edit their own; report managers also curate shared (and orphaned
    shared) templates; a private template is its owner's alone (and admins')."""
    if row is None:
        return False
    if is_admin or is_owner(row, user):
        return True
    return is_manager and row.visibility == "shared"


def serialize_template(row: Any, user: Any, is_admin: bool, is_manager: bool) -> dict:
    editable = can_edit_template(row, user, is_admin, is_manager)
    return {
        "id": row.id, "name": row.name, "description": row.description or "",
        "sections": [e["id"] for e in template_entries(row)],
        "entries": template_entries(row),
        "category": row.category, "tags": list(row.tags or []),
        "visibility": row.visibility, "source_key": row.source_key,
        "default_period": row.default_period,
        "owner_id": row.owner_id, "owner_name": row.owner_name,
        "is_owner": is_owner(row, user),
        "can_edit": editable, "can_delete": editable,
        "can_change_visibility": is_admin or is_owner(row, user),
        "schedules": int(row.schedules or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
