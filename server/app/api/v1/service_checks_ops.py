"""Services review routes: incidents, latest probes, per-check maintenance,
notes, history, reports, bulk operations and portable import.

Mounted BEFORE service_checks.router so the fixed paths (/report, /bulk,
/import) are not captured by GET /service-checks/{check_id}. Every route
applies the caller's tag scope; an out-of-scope id is a 404.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.service_check import ServiceCheck, ServiceCheckGroup, ServiceCheckNote
from app.models.user import User
from app.schemas.service_check import ServiceCheckNoteCreate, ServiceMaintenanceCreate
from app.services import audit_service, service_check_service, service_maintenance, service_metric_service
from app.services.service_check_service import ServiceCheckValidationError

router = APIRouter(prefix="/service-checks", tags=["Service Checks"])

BULK_TEST_CAP = 25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _window(from_time, to_time, default: timedelta) -> tuple[datetime, datetime]:
    if (from_time is None) != (to_time is None):
        raise HTTPException(status_code=422, detail="Pass both 'from' and 'to', or neither")
    if from_time is None:
        end = _now()
        return end - default, end
    start, end = _aware(from_time), _aware(to_time)
    if end <= start:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")
    return start, end


def _zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid reporting timezone")
    return name


async def _scope(db, user):
    return await scoping.visible_tags(db, user)


async def _visible(db, check_id, user):
    sc = await service_check_service.get_visible_check(db, check_id, await _scope(db, user))
    if sc is None:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


def _attachment(body: str, filename: str, media_type: str) -> Response:
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Fleet routes (fixed paths first) ────────────────────────────────────────

@router.get("/report")
async def services_fleet_report(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    format: Literal["json", "csv"] = Query(default="json"),
    timezone_name: str = Query(default="UTC", alias="timezone", max_length=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One row per visible check: confirmed availability, probe success,
    coverage, maintenance, downtime, incidents, target and compliance."""
    from app.services import service_reports
    start, end = _window(from_time, to_time, timedelta(days=30))
    tz = _zone(timezone_name)
    report = await service_reports.fleet_report(db, start, end, tz, await _scope(db, current_user))
    stamp = start.strftime("%Y%m%d")
    if format == "csv":
        return _attachment(service_reports.fleet_report_csv(report), f"services-report-{stamp}.csv", "text/csv")
    return JSONResponse(content=json.loads(json.dumps(report, default=str)),
                        headers={"Content-Disposition": f'attachment; filename="services-report-{stamp}.json"'})


class BulkMaintenance(BaseModel):
    duration_minutes: int = Field(..., ge=1, le=10080)
    reason: Optional[str] = Field(default=None, max_length=2000)


class BulkRequest(BaseModel):
    ids: list[UUID] = Field(..., min_length=1, max_length=500)
    action: Literal["pause", "resume", "test", "add_tags", "remove_tags", "set_group", "maintenance", "delete"]
    tags: Optional[list[str]] = None
    group_id: Optional[UUID] = None
    maintenance: Optional[BulkMaintenance] = None


@router.post("/bulk")
async def bulk_service_check_action(
    data: BulkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    """All ids must be visible (else 404, nothing done); per-item results."""
    from app.services import tag_service

    scope = await _scope(db, current_user)
    ids = list(dict.fromkeys(data.ids))
    rows = await service_check_service.get_visible_checks(db, ids, scope)
    if rows is None:
        raise HTTPException(status_code=404, detail="Service check not found")
    if data.action == "test" and len(rows) > BULK_TEST_CAP:
        raise HTTPException(status_code=422, detail=f"Test at most {BULK_TEST_CAP} monitors at a time")
    if data.action in ("add_tags", "remove_tags") and not data.tags:
        raise HTTPException(status_code=422, detail="tags is required for this action")
    if data.action == "maintenance" and data.maintenance is None:
        raise HTTPException(status_code=422, detail="maintenance is required for this action")
    if data.action == "set_group" and data.group_id is not None:
        if (await db.execute(select(ServiceCheckGroup.id).where(ServiceCheckGroup.id == data.group_id))).scalar_one_or_none() is None:
            raise HTTPException(status_code=422, detail="Group not found")

    results = []
    canonical = await tag_service.canonicalize_tags(db, data.tags) if data.tags else []

    async def audit(sc_id, name, extra=None):
        await audit_service.write_audit_log(
            db, actor=current_user, action=f"service_check.bulk.{data.action}", resource_type="service_check",
            resource_id=str(sc_id), metadata={"name": name, **(extra or {})},
        )

    if data.action == "test":
        from app.api.v1.service_checks import test_service_check
        for sc in rows:
            sc_id, name = sc.id, sc.name
            try:
                out = await test_service_check(check_id=sc_id, db=db, current_user=current_user)
                results.append({"id": str(sc_id), "name": name, "ok": True, "status": out.get("status"),
                                "error": out.get("error") or None})
            except HTTPException as exc:
                results.append({"id": str(sc_id), "name": name, "ok": False, "error": str(exc.detail)})
        return _bulk_out(results)

    for sc in rows:
        sc_id, name = sc.id, sc.name
        try:
            if data.action in ("pause", "resume"):
                before = bool(sc.enabled)
                sc.enabled = data.action == "resume"
                if before != sc.enabled:
                    sc.verification_attempt = 0
                await audit(sc_id, name, {"changes": {"enabled": {"old": before, "new": sc.enabled}}})
            elif data.action in ("add_tags", "remove_tags"):
                before = list(sc.tags or [])
                if data.action == "add_tags":
                    lower = {t.lower() for t in before}
                    after = before + [t for t in canonical if t.lower() not in lower]
                else:
                    drop = {t.lower() for t in canonical}
                    after = [t for t in before if t.lower() not in drop]
                if scope is not None and not scoping.entity_visible(after, scope):
                    results.append({"id": str(sc_id), "name": name, "ok": False,
                                    "error": "tags must include one of your scope tags"})
                    continue
                sc.tags = after
                await audit(sc_id, name, {"changes": {"tags": {"old": before, "new": after}}})
            elif data.action == "set_group":
                before = sc.group_id
                sc.group_id = data.group_id
                await audit(sc_id, name, {"changes": {"group_id": {"old": str(before) if before else None,
                                                                   "new": str(data.group_id) if data.group_id else None}}})
            elif data.action == "maintenance":
                m = await service_maintenance.create_window(
                    db, ServiceMaintenanceCreate(scope_type="check", scope_check_id=sc_id,
                                                 duration_minutes=data.maintenance.duration_minutes,
                                                 reason=data.maintenance.reason),
                    current_user.id, scope)
                await audit(sc_id, name, {"maintenance_id": str(m.id), "ends_at": str(m.ends_at),
                                          "reason": m.reason})
            elif data.action == "delete":
                from sqlalchemy import delete as sql_delete
                await db.execute(sql_delete(ServiceCheck).where(ServiceCheck.id == sc_id))
                await audit(sc_id, name)
            results.append({"id": str(sc_id), "name": name, "ok": True})
        except service_maintenance.MaintenanceError as exc:
            results.append({"id": str(sc_id), "name": name, "ok": False, "error": exc.detail})
        except ValueError as exc:
            results.append({"id": str(sc_id), "name": name, "ok": False, "error": str(exc)})
    await db.commit()
    service_check_service.clear_fleet_cache()
    return _bulk_out(results)


def _bulk_out(results: list[dict]) -> dict:
    ok = sum(1 for r in results if r["ok"])
    return {"results": results, "succeeded": ok, "failed": len(results) - ok}


@router.post("/import")
async def import_service_checks(
    document: dict = Body(...),
    dry_run: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    """Portable (schema_version 2) import; dry_run defaults to true."""
    from app.services import service_reports
    try:
        out = await service_reports.import_checks(db, document, current_user, await _scope(db, current_user),
                                                  dry_run=dry_run)
    except ServiceCheckValidationError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    if dry_run:
        await db.rollback()
        return out
    await audit_service.write_audit_log(
        db, actor=current_user, action="service_check.import", resource_type="service_check",
        resource_id=None, metadata={"summary": out["summary"],
                                    "created": [r["name"] for r in out["results"] if r["action"] == "create"]},
    )
    await db.commit()
    service_check_service.clear_fleet_cache()
    return out


# ── Per-check routes ────────────────────────────────────────────────────────

@router.get("/{check_id}/incidents")
async def get_service_check_incidents(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Incidents from recorded transitions, including the state entering the window."""
    from app.core.database import get_clickhouse_client
    from app.services import service_incidents
    sc = await _visible(db, check_id, current_user)
    start, end = _window(from_time, to_time, timedelta(days=30))
    return await asyncio.to_thread(
        service_incidents.get_incidents, get_clickhouse_client(), sc, start, end, skip, limit)


@router.get("/{check_id}/latest-probes")
async def get_service_check_latest_probes(
    check_id: UUID,
    limit: int = Query(default=6, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Newest individual probes, independent of any chart range (B13)."""
    await _visible(db, check_id, current_user)
    points = await asyncio.to_thread(service_metric_service.get_latest_probes, check_id, limit)
    return {"data": [p.model_dump(mode="json") for p in points], "server_time": _now().isoformat()}


@router.get("/{check_id}/maintenance")
async def get_service_check_maintenance(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every window of any scope (check/group/tag/all) covering this check."""
    sc = await _visible(db, check_id, current_user)
    if from_time is None and to_time is None:
        now = _now()
        start, end = now - timedelta(days=30), now + timedelta(days=30)
    else:
        start, end = _window(from_time, to_time, timedelta(days=30))
    return {"data": await service_maintenance.windows_for_check(db, sc, start, end),
            "server_time": _now().isoformat()}


def _note_out(n) -> dict:
    return {
        "id": str(n.id),
        "incident_started_at": _aware(n.incident_started_at).isoformat() if n.incident_started_at else None,
        "assignee": n.assignee,
        "body": n.body,
        "author_name": n.author_name,
        "created_at": _aware(n.created_at).isoformat() if n.created_at else None,
    }


@router.get("/{check_id}/notes")
async def list_service_check_notes(
    check_id: UUID,
    incident_started_at: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _visible(db, check_id, current_user)
    q = select(ServiceCheckNote).where(ServiceCheckNote.service_check_id == check_id)
    if incident_started_at is not None:
        q = q.where(ServiceCheckNote.incident_started_at == _aware(incident_started_at))
    rows = (await db.execute(q.order_by(ServiceCheckNote.created_at.desc()).limit(500))).scalars().all()
    return {"data": [_note_out(n) for n in rows]}


@router.post("/{check_id}/notes", status_code=201)
async def create_service_check_note(
    check_id: UUID,
    data: ServiceCheckNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    sc = await _visible(db, check_id, current_user)
    note = ServiceCheckNote(
        service_check_id=check_id,
        incident_started_at=_aware(data.incident_started_at),
        assignee=data.assignee,
        body=data.body.strip(),
        author_id=current_user.id,
        author_name=getattr(current_user, "full_name", None) or current_user.username,
        created_at=_now(),
    )
    db.add(note)
    await db.flush()
    await audit_service.write_audit_log(
        db, actor=current_user, action="service_check.note", resource_type="service_check",
        resource_id=str(check_id),
        metadata={"name": sc.name, "note_id": str(note.id), "assignee": data.assignee,
                  "incident_started_at": note.incident_started_at.isoformat() if note.incident_started_at else None},
    )
    await db.commit()
    return _note_out(note)


@router.get("/{check_id}/history")
async def get_service_check_history(
    check_id: UUID,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Configuration/audit history of this check (and its check-scoped maintenance)."""
    await _visible(db, check_id, current_user)
    where = ("((resource_type = 'service_check' AND resource_id = :id) OR "
             "(resource_type = 'service_check_maintenance' AND metadata->>'scope_check_id' = :id))")
    params = {"id": str(check_id)}
    total = (await db.execute(text(f"SELECT COUNT(*) FROM audit_logs WHERE {where}"), params)).scalar() or 0
    rows = (await db.execute(text(
        f"SELECT id, created_at, actor_username, actor_role, action, resource_type, metadata "
        f"FROM audit_logs WHERE {where} ORDER BY created_at DESC OFFSET :skip LIMIT :limit"
    ), {**params, "skip": skip, "limit": limit})).mappings().all()
    data = []
    for r in rows:
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        data.append({
            "id": str(r["id"]),
            "at": _aware(r["created_at"]).isoformat() if r["created_at"] else None,
            "actor": r["actor_username"],
            "actor_role": r["actor_role"],
            "action": r["action"],
            "resource_type": r["resource_type"],
            "changes": meta.get("changes"),
            "metadata": {k: v for k, v in meta.items() if k != "changes"},
        })
    return {"data": data, "meta": {"total": int(total), "skip": skip, "limit": limit}}


@router.get("/{check_id}/report")
async def get_service_check_report(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    format: Literal["json", "csv"] = Query(default="json"),
    timezone_name: str = Query(default="UTC", alias="timezone", max_length=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Selected-window evidence for one check (confirmed basis, maintenance excluded)."""
    from app.services import service_reports
    sc = await _visible(db, check_id, current_user)
    start, end = _window(from_time, to_time, timedelta(days=30))
    tz = _zone(timezone_name)
    try:
        report = await service_reports.check_report(db, sc, start, end, tz, await _scope(db, current_user))
    except ServiceCheckValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if report.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Service check not found")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in sc.name)[:60] or "service"
    stamp = start.strftime("%Y%m%d")
    if format == "csv":
        return _attachment(service_reports.check_report_csv(report), f"{safe}-{stamp}.csv", "text/csv")
    return JSONResponse(content=json.loads(json.dumps(report, default=str)),
                        headers={"Content-Disposition": f'attachment; filename="{safe}-{stamp}.json"'})
