"""Tech-support bundle API endpoints.

Owned by Settings → General → Support in the dashboard. All operations are
admin-only. The router is intentionally thin: bundle generation runs in a
separate root systemd unit (see ``support/__main__.py``), so this file only
handles enqueue, status, list, download, delete, and the corresponding audit
log entries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services import support_jobs
from app.services.audit_service import write_audit_log


logger = logging.getLogger("zenplus.support.api")

router = APIRouter(prefix="/support", tags=["Support"])


class CreateBundleRequest(BaseModel):
    """User-supplied options for a new bundle.

    Validated again in ``support_jobs.enqueue_job`` so this model can be
    relaxed independently from the on-disk request schema if needed.
    """

    issue_category: str = Field(default="other", max_length=64)
    issue_summary: str = Field(default="", max_length=500)
    time_range: str = Field(default="24h")
    include_extended_logs: bool = False


class BundleStatus(BaseModel):
    id: str
    status: str
    phase: str
    created_at: str | None = None
    completed_at: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    filename: str | None = None
    requested_by: str | None = None
    error: str = ""
    request: dict[str, Any] | None = None


@router.post("/bundles", status_code=202, response_model=BundleStatus)
async def create_bundle(
    payload: CreateBundleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
) -> BundleStatus:
    """Enqueue a new bundle job. Returns 202 + job state immediately.

    The dashboard then polls ``GET /bundles/{id}`` every 2 seconds while
    ``status`` is ``queued`` or ``running``.
    """
    try:
        state = support_jobs.enqueue_job(
            issue_category=payload.issue_category,
            issue_summary=payload.issue_summary,
            time_range=payload.time_range,
            include_extended_logs=payload.include_extended_logs,
            requested_by=user.username or str(user.id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await write_audit_log(
        db,
        actor=user,
        action="support_bundle.generate",
        resource_type="support_bundle",
        resource_id=state["id"],
        metadata={
            "issue_category": payload.issue_category,
            "time_range": payload.time_range,
            "include_extended_logs": payload.include_extended_logs,
        },
    )
    await db.commit()
    return BundleStatus(**state)


@router.get("/bundles", response_model=list[BundleStatus])
async def list_bundles(
    user: User = Depends(require_admin_user),
) -> list[BundleStatus]:
    return [BundleStatus(**s) for s in support_jobs.list_jobs(limit=25)]


@router.get("/bundles/{bundle_id}", response_model=BundleStatus)
async def get_bundle(
    bundle_id: str,
    user: User = Depends(require_admin_user),
) -> BundleStatus:
    if not support_jobs.is_valid_job_id(bundle_id):
        raise HTTPException(status_code=400, detail="invalid bundle id")
    state = support_jobs.get_status(bundle_id)
    if state is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    return BundleStatus(**state)


@router.get("/bundles/{bundle_id}/download")
async def download_bundle(
    bundle_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
) -> Response:
    if not support_jobs.is_valid_job_id(bundle_id):
        raise HTTPException(status_code=400, detail="invalid bundle id")
    state = support_jobs.get_status(bundle_id)
    if state is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if state.get("status") != support_jobs.STATUS_READY:
        raise HTTPException(status_code=409, detail=f"bundle not ready: {state.get('status')}")

    bundle_file: Path = support_jobs.bundle_path(bundle_id)
    if not bundle_file.exists():
        raise HTTPException(status_code=410, detail="bundle file missing on disk")

    filename = state.get("filename") or f"zenplus-support-{bundle_id}.tar.gz"

    await write_audit_log(
        db,
        actor=user,
        action="support_bundle.download",
        resource_type="support_bundle",
        resource_id=bundle_id,
        metadata={"filename": filename, "size_bytes": state.get("size_bytes", 0)},
    )
    await db.commit()

    return FileResponse(
        path=str(bundle_file),
        media_type="application/gzip",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/bundles/{bundle_id}", status_code=204)
async def delete_bundle(
    bundle_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
) -> Response:
    if not support_jobs.is_valid_job_id(bundle_id):
        raise HTTPException(status_code=400, detail="invalid bundle id")
    if support_jobs.get_status(bundle_id) is None:
        raise HTTPException(status_code=404, detail="bundle not found")

    support_jobs.delete_job(bundle_id)
    await write_audit_log(
        db,
        actor=user,
        action="support_bundle.delete",
        resource_type="support_bundle",
        resource_id=bundle_id,
    )
    await db.commit()
    return Response(status_code=204)
