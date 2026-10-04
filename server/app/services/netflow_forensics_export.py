"""Background NetFlow forensics exports (contract §9.4: ``netflow_export_loop``).

``POST /netflow/forensics/export-jobs`` queues a job whose ``query`` pins the
search exactly (canonical filter, fixed window, received-at snapshot, sort,
columns). This loop runs the queued jobs one at a time and writes a gzipped CSV
under ``NETFLOW_EXPORT_DIR`` (default /opt/zenplus/data/netflow-exports):

* the records are read with the same keyset chunks as the streamed export, so
  the file holds exactly the rows the search matched when it was requested;
* the requester's visibility and permissions are re-checked when the job
  starts (a revoked ``netflow.export`` or a narrowed scope applies to queued
  jobs too);
* progress (``row_count``) is written after every chunk, and the job stops at
  the next chunk when it is cancelled;
* finished files expire after NETFLOW_EXPORT_RETENTION_HOURS; the loop marks
  the job ``expired`` and deletes the file. Files of failed, cancelled and
  removed jobs are deleted as well, and ``.part`` leftovers of an interrupted
  run are cleaned up;
* a job left ``running`` by a process that died is re-queued once, then failed.

Two uvicorn workers both start the loop: a session-level PostgreSQL advisory
lock makes one of them the runner, and jobs are claimed with ``FOR UPDATE SKIP
LOCKED`` so a job is never run twice.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.models.netflow_governance import NetflowExportJob
from app.models.user import User

logger = logging.getLogger("zenplus.netflow.export")

NETFLOW_EXPORT_ADVISORY_LOCK = 1515074437
POLL_S = 5.0
STARTUP_DELAY_S = 10.0
MAX_ATTEMPTS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _forensics():
    # Imported lazily: the API package imports the whole NetFlow router.
    from app.api.v1.netflow import forensics
    return forensics


class _Runner:
    """Holds the advisory lock on one dedicated session while it runs jobs."""

    def __init__(self) -> None:
        self._cm = None
        self.db = None
        self.locked = False

    async def __aenter__(self) -> "_Runner":
        self._cm = AsyncSessionLocal()
        self.db = await self._cm.__aenter__()
        self.locked = bool((await self.db.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": NETFLOW_EXPORT_ADVISORY_LOCK}
        )).scalar())
        await self.db.commit()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self.locked:
                await self.db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": NETFLOW_EXPORT_ADVISORY_LOCK})
                await self.db.commit()
        except Exception:
            logger.debug("netflow export: unlock failed", exc_info=True)
        finally:
            await self._cm.__aexit__(exc_type, exc, tb)


async def _set(job_id: uuid.UUID, **values) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(NetflowExportJob, job_id)
        if job is None:
            return
        for k, v in values.items():
            setattr(job, k, v)
        await db.commit()


async def _status(job_id: uuid.UUID) -> str | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            text("SELECT status FROM netflow_export_jobs WHERE id = :id"), {"id": job_id}
        )).scalar()


async def housekeeping(now: datetime | None = None) -> dict:
    """Expire finished files, delete orphaned files, re-queue or fail jobs left
    running by a dead process. Returns counts (for tests and logs)."""
    fx = _forensics()
    now = now or _now()
    out = {"expired": 0, "requeued": 0, "failed": 0, "removed_files": 0}
    base = fx.export_dir()
    async with AsyncSessionLocal() as db:
        expired = (await db.execute(text(
            "UPDATE netflow_export_jobs SET status = 'expired' "
            "WHERE status = 'done' AND expires_at IS NOT NULL AND expires_at <= :now RETURNING id, file_path"
        ), {"now": now})).all()
        out["expired"] = len(expired)
        for job_id, file_path in expired:
            if file_path:
                fx._remove_file(file_path, job_id)
        # Only the advisory-lock holder runs jobs and housekeeping runs before
        # it claims one, so any job still 'running' here was left by a
        # process that died (or lost its lock) mid-export.
        stale = (await db.execute(text(
            "SELECT id, query FROM netflow_export_jobs WHERE status = 'running' FOR UPDATE SKIP LOCKED"
        ))).all()
        for job_id, query in stale:
            attempts = int((query or {}).get("attempts") or 0)
            if attempts + 1 >= MAX_ATTEMPTS:
                await db.execute(text(
                    "UPDATE netflow_export_jobs SET status = 'failed', finished_at = :now, "
                    "error = 'The export was interrupted and could not be completed; run it again' WHERE id = :id"
                ), {"now": now, "id": job_id})
                out["failed"] += 1
            else:
                await db.execute(text(
                    "UPDATE netflow_export_jobs SET status = 'queued', started_at = NULL, row_count = 0, "
                    "query = jsonb_set(query, '{attempts}', to_jsonb(CAST(:a AS int))) WHERE id = :id"
                ), {"a": attempts + 1, "id": job_id})
                out["requeued"] += 1
        keep = set((await db.execute(text(
            "SELECT file_path FROM netflow_export_jobs WHERE status = 'done' AND file_path IS NOT NULL"
        ))).scalars().all())
        running = set(str(i) for i in (await db.execute(text(
            "SELECT id FROM netflow_export_jobs WHERE status IN ('queued', 'running')"
        ))).scalars().all())
        await db.commit()
    if base.is_dir():
        for p in base.iterdir():
            if not p.is_file() or not p.name.endswith((".csv.gz", ".csv.gz.part")):
                continue
            job_part = p.name.split(".", 1)[0]
            if str(p) in keep or job_part in running:
                continue
            try:
                p.unlink()
                out["removed_files"] += 1
            except OSError:
                logger.warning("netflow export: could not remove %s", p, exc_info=True)
    return out


async def _claim() -> uuid.UUID | None:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text(
            "SELECT id FROM netflow_export_jobs WHERE status = 'queued' "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        ))).first()
        if row is None:
            await db.commit()
            return None
        await db.execute(text(
            "UPDATE netflow_export_jobs SET status = 'running', started_at = :now, row_count = 0, error = NULL "
            "WHERE id = :id"
        ), {"now": _now(), "id": row[0]})
        await db.commit()
        return row[0]


async def run_job(job_id: uuid.UUID) -> str:
    """Run one claimed job to completion; returns its final status."""
    from app.api.v1.netflow.access import EXPORT_PERMISSION, VIEW_PERMISSION, visibility_for_user
    from app.api.v1.netflow.common import resolve_window
    from app.api.v1.netflow.filters import parse_flow_filter

    fx = _forensics()
    async with AsyncSessionLocal() as db:
        job = await db.get(NetflowExportJob, job_id)
        if job is None:
            return "missing"
        q = dict(job.query or {})
        user = await db.get(User, job.requested_by) if job.requested_by else None
        if user is None or not user.is_active:
            await db.rollback()
            await _set(job_id, status="failed", finished_at=_now(), error="The requesting user no longer exists")
            return "failed"
        vis = await visibility_for_user(db, user)
        names = await fx.all_interface_names(db)
        await db.commit()
    if not (vis.can(EXPORT_PERMISSION) and vis.can(VIEW_PERMISSION)):
        await _set(job_id, status="failed", finished_at=_now(),
                   error="The requester no longer holds the netflow.export permission")
        return "failed"
    try:
        f = parse_flow_filter({**(q.get("filter") or {}), **({"tz": q["tz"]} if q.get("tz") else {})})
        w = q.get("window") or {}
        win = resolve_window(from_ts=w.get("effective_from") or w.get("from"), to_ts=w.get("to"),
                             tz=w.get("tz"), max_span=fx.MAX_SPAN_RAW, record=False,
                             now=_now() + timedelta(days=1))
        cols = fx.parse_columns(",".join(q.get("columns") or []) or None)
        spec = fx.SearchSpec(start=win.start, end=win.end, snapshot_ms=int(q["snapshot_ms"]), filter=f, vis=vis,
                             sort=q.get("sort") or "timestamp", order=q.get("order") or "desc")
        if spec.sort not in fx.SORTS or spec.order not in fx.ORDERS:
            raise ValueError("sort")
    except Exception as exc:
        logger.warning("netflow export %s: invalid stored query: %s", job_id, exc)
        await _set(job_id, status="failed", finished_at=_now(), error="The stored export query is no longer valid")
        return "failed"
    if f.exporter and not vis.allows(f.exporter):
        await _set(job_id, status="failed", finished_at=_now(), error="The exporter is no longer visible to the requester")
        return "failed"

    base = fx.export_dir()
    final = base / f"{job_id}.csv.gz"
    part = base / f"{job_id}.csv.gz.part"
    max_rows = min(int(q.get("total") or fx.EXPORT_JOB_MAX_ROWS), fx.EXPORT_JOB_MAX_ROWS) \
        if q.get("total_exact") else fx.EXPORT_JOB_MAX_ROWS
    written = 0
    try:
        # Inside the try: a directory that cannot be created fails this job
        # with a logged reason instead of leaving it 'running'.
        base.mkdir(parents=True, exist_ok=True, mode=0o750)
        with gzip.open(part, "wt", encoding="utf-8", newline="") as fh:
            fh.write(fx.csv_header(cols))
            async for chunk in fx.iter_export_rows(spec, max_rows):
                items = [fx.format_row(r) for r in chunk]
                await asyncio.to_thread(fh.write, fx.csv_lines(items, cols, names))
                written += len(items)
                if await _status(job_id) != "running":
                    raise asyncio.CancelledError()
                await _set(job_id, row_count=written)
        os.replace(part, final)
    except asyncio.CancelledError:
        _discard(part)
        status = await _status(job_id)
        if status == "running":     # the loop itself is being cancelled (shutdown)
            await _set(job_id, status="queued", started_at=None, row_count=0)
            raise
        return status or "cancelled"
    except Exception as exc:
        _discard(part)
        detail = getattr(exc, "detail", None)
        logger.exception("netflow export %s failed", job_id)
        await _set(job_id, status="failed", finished_at=_now(),
                   error=(detail if isinstance(detail, str) else "The export failed; see the server log"))
        return "failed"
    size = final.stat().st_size
    now = _now()
    if await _status(job_id) != "running":
        final.unlink(missing_ok=True)
        return "cancelled"
    await _set(job_id, status="done", finished_at=now, row_count=written, byte_size=size,
               file_path=str(final), expires_at=now + timedelta(hours=fx.EXPORT_JOB_RETENTION_HOURS))
    logger.info("netflow export %s: %d rows, %d bytes", job_id, written, size)
    return "done"


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("netflow export: could not remove %s", path, exc_info=True)


async def run_once() -> int:
    """One pass: housekeeping, then every queued job while holding the lock.
    Returns the number of jobs run (0 when another worker holds the lock)."""
    ran = 0
    async with _Runner() as runner:
        if not runner.locked:
            return 0
        await housekeeping()
        while True:
            job_id = await _claim()
            if job_id is None:
                break
            await run_job(job_id)
            ran += 1
    return ran


async def netflow_export_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow export loop pass failed")
        await asyncio.sleep(POLL_S)


__all__ = ["netflow_export_loop", "run_once", "run_job", "housekeeping", "NETFLOW_EXPORT_ADVISORY_LOCK"]
