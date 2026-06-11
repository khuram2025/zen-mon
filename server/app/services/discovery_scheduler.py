"""Background scheduler for discovery runs.

Scheduled discovery previously only fired when someone clicked the manual
"Run scheduler now" button (POST /discovery-v2/scheduler/tick). This module
makes schedules real: a 60-second loop (started from main.py) evaluates due
schedules and launches their runs in-process, exactly like the endpoint.

It also recovers orphaned runs on startup: scans execute as in-process
asyncio tasks, so an API restart strands anything 'queued'/'running' — those
rows are marked failed so the UI doesn't show eternally spinning runs and the
one-in-flight-per-profile guard doesn't deadlock the schedule forever.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_v2 import DiscoveryRun, DiscoverySchedule
from app.services.discovery_executor import start_run_task

logger = logging.getLogger("zenplus.discovery_scheduler")

TICK_INTERVAL_S = 60


def compute_next_run(s: DiscoverySchedule, *, after: datetime | None = None) -> datetime | None:
    """Cheap, dependency-free next-run calculator."""
    now = after or datetime.now(timezone.utc)
    if not s.enabled or (s.end_date and s.end_date < now):
        return None
    start = s.start_date or now
    if s.schedule_type == "once_now":
        return now
    if s.schedule_type == "once_future":
        return start if start > now else None
    if s.schedule_type == "recurring":
        if s.interval_minutes and s.interval_minutes > 0:
            delta = timedelta(minutes=s.interval_minutes)
        elif s.frequency == "hourly":
            delta = timedelta(hours=1)
        elif s.frequency == "daily":
            delta = timedelta(days=1)
        elif s.frequency == "weekly":
            delta = timedelta(weeks=1)
        elif s.frequency == "monthly":
            delta = timedelta(days=30)
        else:
            delta = timedelta(hours=1)
        nxt = start
        while nxt <= now:
            nxt = nxt + delta
        return nxt
    if s.schedule_type == "cron":
        return s.next_run_at
    return None


async def run_scheduler_tick(db: AsyncSession) -> dict:
    """Start runs for every due, enabled schedule (one in flight per profile)."""
    now = datetime.now(timezone.utc)
    # FOR UPDATE SKIP LOCKED: the API runs multiple workers, each with its own
    # loop — locking due rows keeps a schedule from double-firing.
    rows = (
        await db.execute(
            select(DiscoverySchedule)
            .where(
                DiscoverySchedule.enabled == True,  # noqa: E712
                DiscoverySchedule.next_run_at != None,  # noqa: E711
                DiscoverySchedule.next_run_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    triggered: list[str] = []
    for s in rows:
        in_flight = (
            await db.execute(
                select(DiscoveryRun).where(
                    DiscoveryRun.profile_id == s.profile_id,
                    DiscoveryRun.status.in_(("queued", "running")),
                )
            )
        ).scalars().first()
        if in_flight:
            continue
        run = DiscoveryRun(
            profile_id=s.profile_id,
            schedule_id=s.id,
            trigger_type="scheduled",
            status="queued",
            phase="preparing",
        )
        db.add(run)
        await db.flush()
        s.last_run_at = now
        s.last_run_id = run.id
        if s.schedule_type in ("once_now", "once_future"):
            s.enabled = False
            s.next_run_at = None
        else:
            s.next_run_at = compute_next_run(s, after=now + timedelta(seconds=1))
        triggered.append(str(run.id))
        start_run_task(run.id)
    await db.commit()
    if triggered:
        logger.info("scheduler tick: started %d run(s): %s", len(triggered), triggered)
    return {"triggered": triggered, "checked": len(rows)}


async def recover_stuck_runs(db: AsyncSession) -> int:
    """Mark runs orphaned by a restart as failed (their tasks died with the process)."""
    res = await db.execute(
        text("""UPDATE discovery_runs
                SET status = 'failed', phase = 'done',
                    error_details = 'Interrupted by API restart',
                    completed_at = NOW()
                WHERE status IN ('queued', 'running')"""),
    )
    await db.commit()
    n = res.rowcount or 0
    if n:
        logger.warning("recovered %d discovery run(s) stranded by restart", n)
    return n


async def discovery_scheduler_loop() -> None:
    """Background task: recover stranded runs once, then tick every minute."""
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(15)  # let the app settle before touching the DB
    try:
        async with AsyncSessionLocal() as db:
            await recover_stuck_runs(db)
    except Exception:
        logger.exception("stuck-run recovery failed")

    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_scheduler_tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("discovery scheduler tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)
