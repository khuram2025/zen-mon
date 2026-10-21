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
MAX_SCOPE_VALUES = 100
MAX_SECTIONS = 40
# A device scope up to this size is named in the scope label; a larger one
# names its first few devices.
SCOPE_NAMES_SHOWN = 5
SCOPE_NAMES_PREVIEW = 3
SCOPE_KEYS = ("group_ids", "locations", "device_types")

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}


# ─── File names ─────────────────────────────────────────────────────────────

def _slug(value: str, limit: int = 48) -> str:
    """ASCII words joined by '-', at most ``limit`` characters. A longer slug
    is cut at a word boundary ("...-Review-1st-08-0" never happens), unless
    the first word alone is longer than the limit."""
    ascii_ = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    ascii_ = re.sub(r"[^A-Za-z0-9]+", "-", ascii_).strip("-")
    if len(ascii_) <= limit:
        return ascii_
    cut = ascii_[:limit]
    if ascii_[limit] != "-" and "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-")


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


def split_ids(raw: Any) -> Any:
    """Repeated id query values, each possibly comma-separated, as one flat
    list (ids are UUIDs, which never contain commas). Strings and None pass
    through."""
    if raw is None or isinstance(raw, str):
        return raw
    return [part for item in raw for part in str(item).split(",")]


def parse_scope_values(raw: Any, what: str, *, uuids: bool = False) -> Optional[list[str]]:
    """Group ids, locations or device types (None when empty). A list is
    taken value by value -- a location may contain a comma; a single string
    (the legacy form) is comma-separated."""
    if raw is None or raw == "" or raw == []:
        return None
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    values = list(dict.fromkeys(str(i).strip()[:200] for i in items if str(i).strip()))
    if len(values) > MAX_SCOPE_VALUES:
        raise HTTPException(400, f"Too many {what} (max {MAX_SCOPE_VALUES})")
    if uuids:
        for v in values:
            try:
                _uuid.UUID(v)
            except ValueError:
                raise HTTPException(400, f"{what} must be UUIDs")
    return values or None


async def resolve_device_scope(db: AsyncSession, *, device_ids: Optional[list[str]] = None,
                               group_ids: Optional[list[str]] = None,
                               locations: Optional[list[str]] = None,
                               device_types: Optional[list[str]] = None) -> Optional[list[str]]:
    """Device ids a report scope covers: None = no scope (every device), [] =
    the scope matched nothing (callers must fail closed, never treat it as
    "every device").

    Mirrors the legacy reports (report_service._build_device_filter_sql):
    every given filter must match (device ids AND groups AND locations AND
    device types), each one any of its values."""
    if not (group_ids or locations or device_types):
        return [str(i) for i in device_ids] if device_ids else None
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if device_ids:
        clauses.append("d.id::text = ANY(:device_ids)")
        params["device_ids"] = [str(i) for i in device_ids]
    if group_ids:
        clauses.append("d.group_id::text = ANY(:group_ids)")
        params["group_ids"] = [str(i) for i in group_ids]
    if locations:
        clauses.append("d.location = ANY(:locations)")
        params["locations"] = list(locations)
    if device_types:
        clauses.append("d.device_type = ANY(:device_types)")
        params["device_types"] = list(device_types)
    rows = (await db.execute(text(
        "SELECT d.id::text FROM devices d WHERE " + " AND ".join(clauses) + " ORDER BY d.hostname"),
        params)).fetchall()
    return [str(r[0]) for r in rows]


async def apply_device_scope(db: AsyncSession, filters: Optional[dict], *,
                             group_ids: Optional[list[str]] = None,
                             locations: Optional[list[str]] = None,
                             device_types: Optional[list[str]] = None) -> Optional[dict]:
    """``filters`` with a group / location / device-type scope resolved into
    ``device_ids`` (resolve_device_scope). The scope itself is kept in the
    filters for the scope label; a scope that matches nothing pins the
    filter to scoping.NO_MATCH_ID, so no section widens to the estate."""
    if not (group_ids or locations or device_types):
        return filters
    out = dict(filters or {})
    ids = await resolve_device_scope(db, device_ids=out.get("device_ids"), group_ids=group_ids,
                                     locations=locations, device_types=device_types)
    for key, values in (("group_ids", group_ids), ("locations", locations), ("device_types", device_types)):
        if values:
            out[key] = list(values)
    out["device_ids"] = ids or [scoping.NO_MATCH_ID]
    return out


def _named(noun: str, plural: str, names: list[str]) -> str:
    if len(names) == 1:
        return f"{noun}: {names[0]}"
    if len(names) <= SCOPE_NAMES_SHOWN:
        return f"{plural}: {', '.join(names)}"
    return f"{len(names)} {plural.lower()} ({', '.join(names[:SCOPE_NAMES_PREVIEW])} …)"


def scope_label_for(filters: Optional[dict], names: Optional[dict] = None) -> str:
    """The scope line of a report ("Group: Core Network", "Amman_New100F,
    Dammam, FortiGate-101F", "12 selected devices (A, B, C …)").

    ``names`` maps ``devices`` / ``groups`` to display names, in the order
    to show (scope_label looks them up); without them devices and groups
    are counted. A group / location / device-type scope is described as
    such: its ``device_ids`` are the resolved devices, not a selection."""
    f = filters or {}
    names = names or {}
    bits = []
    if any(f.get(k) for k in SCOPE_KEYS):
        if f.get("group_ids"):
            groups = names.get("groups") or []
            bits.append(_named("Group", "Groups", groups) if len(groups) == len(f["group_ids"])
                        else f"{len(f['group_ids'])} device group(s)")
        if f.get("locations"):
            bits.append(_named("Location", "Locations", [str(v) for v in f["locations"]]))
        if f.get("device_types"):
            bits.append(_named("Device type", "Device types", [str(v) for v in f["device_types"]]))
    elif f.get("device_ids"):
        devices = names.get("devices") or []
        n = len(f["device_ids"])
        if devices and len(devices) == n and n <= SCOPE_NAMES_SHOWN:
            bits.append(", ".join(devices))
        elif devices and len(devices) == n:
            bits.append(f"{n} selected devices ({', '.join(devices[:SCOPE_NAMES_PREVIEW])} …)")
        else:
            bits.append(f"{n} selected device{'s' if n != 1 else ''}")
    if f.get("application_id"):
        bits.append(f"application “{f['application_id']}”")
    if f.get("netflow"):
        from app.services import netflow_report
        bits.append(f"NetFlow: {netflow_report.describe(f['netflow'])}")
    return " · ".join(bits) if bits else "All monitored infrastructure"


async def scope_label(db: AsyncSession, filters: Optional[dict]) -> str:
    """scope_label_for with device and group names looked up (the label of
    the viewer, downloads, previews and scheduled runs)."""
    f = filters or {}
    names: dict[str, list[str]] = {}
    try:
        if f.get("group_ids"):
            rows = (await db.execute(text(
                "SELECT id::text, name FROM device_groups WHERE id::text = ANY(:ids) ORDER BY name"),
                {"ids": [str(i) for i in f["group_ids"]]})).fetchall()
            names["groups"] = [str(r[1] or r[0]) for r in rows]
        elif f.get("device_ids") and not any(f.get(k) for k in SCOPE_KEYS):
            rows = (await db.execute(text(
                "SELECT id::text, hostname, ip_address::text FROM devices WHERE id::text = ANY(:ids) "
                "ORDER BY hostname"), {"ids": [str(i) for i in f["device_ids"]]})).fetchall()
            names["devices"] = [str(r[1] or (r[2] or "").split("/")[0] or r[0][:8]) for r in rows]
    except Exception:
        # A label is never worth failing a report: fall back to counts.
        import logging
        logging.getLogger("zenplus.reports").debug("scope names unavailable", exc_info=True)
        names = {}
    return scope_label_for(f, names)


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

    label = await scope_label(db, filters)
    if scoped:
        label = "Limited to your visibility scope" + (
            f" ({omitted} fleet-wide section(s) omitted)" if omitted else "")
    meta = await rs.build_report_meta(db, title, start, end, description=description, category=category,
                                      scope_label=label, branding=branding)

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
            "scope_label": label,
            "scoped": scoped,
            "omitted_sections": omitted,
            "restricted_sections": sum(1 for s in sections if s.get("restricted")),
            "filters": {k: v for k, v in (filters or {}).items()
                        if k in ("netflow", "application_id", *SCOPE_KEYS)},
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
    elif fmt == "csv":
        from app.services.report_csv import render_csv
        body = await asyncio.to_thread(render_csv, meta, sections)
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
