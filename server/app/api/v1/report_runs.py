"""Delivered reports (``report_runs``) and the report delivery settings.

The run history across schedules with paging and filters, the stored
attachment of a run, revoking a run's share link, retrying the deliveries a
run still owes, and the organisation settings for link lifetime, retention,
attachment size and retries (docs/reports-delivery.md).

Share tokens and attachments follow the same rule: the schedule's owner and
report managers (``report_schedules.is_report_manager``) get them; a run
whose schedule was deleted, report managers only. Accounts limited to a
visibility scope only see the runs of schedules they own.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import has_permission
from app.core.security import get_role_permissions, require_permission
from app.models.user import User
from app.services import report_settings
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/reports", tags=["Reports"])

RUN_STATUSES = ("running", "success", "partial", "failed", "skipped")
RUN_COLUMNS = (
    "r.id, r.schedule_id, r.title, r.report_type, r.period, r.token, r.status, r.delivered_to, "
    "r.error, r.generated_at, r.expires_at, r.kind, r.notes, r.failed_targets, r.retry_count, "
    "r.next_retry_at, r.artifact_name, (r.artifact IS NOT NULL) AS has_artifact, "
    "s.name AS schedule_name, s.created_by AS owner_id"
)


_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")


def _redact(note: Any) -> str:
    return _ADDRESS.sub("[address]", str(note))


def serialize_run(r: Any, *, links: bool, manager: bool) -> dict:
    """One run for the API. ``links``: the caller may have the share token
    and attachment. Delivery addresses are listed for report managers and the
    owner (``links``) only."""
    from datetime import datetime, timezone

    expires = r.expires_at
    revoked = expires is not None and expires <= datetime.now(timezone.utc)
    delivered = list(r.delivered_to or [])
    failed = list(r.failed_targets or [])
    return {
        "id": str(r.id),
        "schedule_id": str(r.schedule_id) if r.schedule_id else None,
        "schedule_name": r.schedule_name,
        "title": r.title, "report_type": r.report_type, "period": r.period,
        "token": r.token if links and not revoked else None, "token_withheld": not links,
        "status": r.status, "kind": r.kind or "scheduled",
        "delivered_to": delivered if links else [], "delivered_count": len(delivered),
        "error": r.error if links else (("Delivery problems: " + str(len(failed))) if failed else None),
        "notes": [str(n) if links else _redact(n) for n in (r.notes or [])],
        "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        "expires_at": expires.isoformat() if expires else None,
        "revoked": revoked,
        "has_artifact": bool(r.has_artifact) and links, "artifact_name": r.artifact_name if links else None,
        "retry_count": int(r.retry_count or 0),
        "next_retry_at": r.next_retry_at.isoformat() if r.next_retry_at else None,
        "failed_targets": ([{"kind": t.get("kind"), "name": t.get("name"), "error": t.get("error")}
                            for t in failed] if links else []),
        "can_retry": manager and bool(failed),
    }


async def _caller(db: AsyncSession, user: User) -> tuple[bool, bool, str]:
    """(report manager, limited to a visibility scope, user id)."""
    from app.api.v1.report_schedules import is_report_manager
    return (await is_report_manager(db, user), await scoping.visible_tags(db, user) is not None,
            str(user.id))


async def _load_run(db: AsyncSession, run_id: UUID) -> Any:
    row = (await db.execute(text(
        f"SELECT {RUN_COLUMNS} FROM report_runs r LEFT JOIN report_schedules s ON s.id = r.schedule_id "
        "WHERE r.id = :id"), {"id": run_id})).first()
    if row is None:
        raise HTTPException(404, "Delivered report not found")
    return row


def _disposition(filename: str) -> str:
    import re
    from urllib.parse import quote
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "-", filename).strip("-") or "report"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _owner_or_manager(row: Any, manager: bool, me: str) -> bool:
    return manager or (row.owner_id is not None and str(row.owner_id) == me)


@router.get("/runs")
async def list_report_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100_000),
    schedule_id: Optional[UUID] = None,
    status: Optional[str] = Query(None, pattern="^(running|success|partial|failed|skipped)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    """Generated reports across schedules, newest first: ``{data, total,
    limit, offset}``."""
    manager, scoped, me = await _caller(db, current_user)
    where, params = [], {"lim": limit, "off": offset}
    if schedule_id:
        where.append("r.schedule_id = :sid")
        params["sid"] = schedule_id
    if status:
        where.append("r.status = :st")
        params["st"] = status
    if scoped:
        where.append("s.created_by = CAST(:me AS uuid)")
        params["me"] = me
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    base = "FROM report_runs r LEFT JOIN report_schedules s ON s.id = r.schedule_id"
    total = (await db.execute(text(f"SELECT count(*) {base}{clause}"), params)).scalar() or 0
    rows = (await db.execute(text(
        f"SELECT {RUN_COLUMNS} {base}{clause} ORDER BY r.generated_at DESC LIMIT :lim OFFSET :off"),
        params)).fetchall()
    data = [serialize_run(r, links=_owner_or_manager(r, manager, me), manager=manager) for r in rows]
    return {"data": data, "total": int(total), "limit": limit, "offset": offset}


@router.get("/runs/{run_id}/artifact")
async def download_run_artifact(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    """The attachment exactly as it was emailed (kept for the artifact
    retention period)."""
    perms = await get_role_permissions(db, current_user.role)
    if not has_permission(perms, "reports.export"):
        raise HTTPException(403, "Downloading reports requires the reports.export permission")
    manager, _scoped, me = await _caller(db, current_user)
    row = await _load_run(db, run_id)
    if not _owner_or_manager(row, manager, me):
        raise HTTPException(404, "Delivered report not found")
    art = (await db.execute(text(
        "SELECT artifact, artifact_name, artifact_mime FROM report_runs WHERE id = :id"), {"id": run_id})).first()
    if art is None or art.artifact is None:
        raise HTTPException(404, "This run has no stored attachment (none was sent, or it passed the "
                                 "retention period)")
    return Response(content=bytes(art.artifact), media_type=art.artifact_mime or "application/octet-stream",
                    headers={"Content-Disposition": _disposition(art.artifact_name or "report"),
                             "Cache-Control": "private, no-store"})


@router.post("/runs/{run_id}/revoke")
async def revoke_run_link(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    """Expire a run's share link now (the run stays in the history)."""
    manager, _scoped, me = await _caller(db, current_user)
    row = await _load_run(db, run_id)
    if not _owner_or_manager(row, manager, me):
        raise HTTPException(404, "Delivered report not found")
    await db.execute(text("UPDATE report_runs SET expires_at = NOW() WHERE id = :id"), {"id": run_id})
    await write_audit_log(db, actor=current_user, action="reports.run.revoke_link", resource_type="report_run",
                          resource_id=str(run_id), metadata={"title": row.title})
    await db.commit()
    return serialize_run(await _load_run(db, run_id), links=True, manager=manager)


@router.post("/runs/{run_id}/retry")
async def retry_run_delivery(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    """Send the stored email again, now, to the targets the run still owes."""
    from app.services.report_scheduler import retry_run

    manager, _scoped, _me = await _caller(db, current_user)
    if not manager:
        raise HTTPException(403, "Accounts limited to a visibility scope cannot retry deliveries")
    row = await _load_run(db, run_id)
    if not row.failed_targets:
        raise HTTPException(409, "This run has no failed deliveries to retry")
    claimed = (await db.execute(text(
        "UPDATE report_runs SET next_retry_at = NULL WHERE id = :id AND failed_targets <> '[]'::jsonb "
        "RETURNING id"), {"id": run_id})).first()
    await db.commit()
    if claimed is None:
        raise HTTPException(409, "This run has no failed deliveries to retry")
    result = await retry_run(db, str(run_id), manual=True)
    if result.get("status") == "busy":
        await db.rollback()
        raise HTTPException(409, "A retry of this run is already in progress")
    await write_audit_log(db, actor=current_user, action="reports.run.retry", resource_type="report_run",
                          resource_id=str(run_id),
                          metadata={"delivered": len(result.get("delivered") or []),
                                    "remaining": result.get("remaining")})
    await db.commit()
    return {"result": result, "run": serialize_run(await _load_run(db, run_id), links=True, manager=True)}


@router.get("/settings")
async def get_report_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.view")),
):
    return {"settings": await report_settings.load(db),
            "limits": {k: {"default": d, "min": lo, "max": hi}
                       for k, (d, lo, hi) in report_settings.LIMITS.items()}}


@router.put("/settings")
async def put_report_settings(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("reports.manage")),
):
    manager, _scoped, _me = await _caller(db, current_user)
    if not manager:
        raise HTTPException(403, "Accounts limited to a visibility scope cannot change report settings")
    errors = report_settings.validation_errors(body if isinstance(body, dict) else {})
    if errors:
        raise HTTPException(400, "; ".join(errors))
    before = await report_settings.load(db)
    saved = await report_settings.save(db, body)
    await write_audit_log(db, actor=current_user, action="reports.settings.update", resource_type="settings",
                          resource_id="reports",
                          metadata={k: {"from": before[k], "to": saved[k]} for k in saved if before[k] != saved[k]})
    await db.commit()
    return {"settings": saved}
