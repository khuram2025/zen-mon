"""Tech-support bundle API endpoints.

Owned by Settings → General → Support in the dashboard. All operations are
admin-only. The router is intentionally thin: bundle generation runs in a
separate unprivileged systemd worker (see ``support/__main__.py``), so this file only
handles enqueue, status, list, download, delete, and the corresponding audit
log entries.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
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
    skipped_files: list[str] = Field(default_factory=list)
    truncated_files: list[str] = Field(default_factory=list)
    collector_failures: list[str] = Field(default_factory=list)
    collector_warnings: list[str] = Field(default_factory=list)
    bundle_schema_version: int | None = None
    worker_version: str | None = None


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
    except support_jobs.SupportQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
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

    try:
        bundle_stream, actual_size = support_jobs.open_bundle_for_download(
            bundle_id,
            expected_size=state.get("size_bytes"),
            expected_sha256=state.get("sha256"),
        )
    except support_jobs.InvalidBundleFileError as exc:
        logger.warning("refusing unsafe/corrupt support bundle %s: %s", bundle_id, exc)
        raise HTTPException(
            status_code=410, detail="bundle file is missing or failed its integrity check"
        ) from exc

    filename = support_jobs.safe_download_filename(state.get("filename"), bundle_id)

    try:
        await write_audit_log(
            db,
            actor=user,
            action="support_bundle.download",
            resource_type="support_bundle",
            resource_id=bundle_id,
            metadata={"filename": filename, "size_bytes": actual_size},
        )
        await db.commit()
    except Exception:
        bundle_stream.close()
        raise

    def chunks():
        try:
            while True:
                chunk = bundle_stream.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            bundle_stream.close()

    return StreamingResponse(
        chunks(),
        media_type="application/gzip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(actual_size),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(bundle_stream.close),
    )


@router.delete("/bundles/{bundle_id}", status_code=204)
async def delete_bundle(
    bundle_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
) -> Response:
    if not support_jobs.is_valid_job_id(bundle_id):
        raise HTTPException(status_code=400, detail="invalid bundle id")
    state = support_jobs.get_status(bundle_id)
    if state is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if state.get("status") in (support_jobs.STATUS_QUEUED, support_jobs.STATUS_RUNNING):
        raise HTTPException(status_code=409, detail="cannot delete a running support bundle")

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
