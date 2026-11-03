"""Background scheduler for discovery runs.

Scheduled discovery previously only fired when someone clicked the manual
"Run scheduler now" button (POST /discovery-v2/scheduler/tick). This module
makes schedules real: a 60-second loop (started from main.py) evaluates due
schedules and launches their runs in-process, exactly like the endpoint.

Every tick recovers expired running work and launches committed queued work.
A new worker must never cancel scans owned by a healthy worker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_v2 import DiscoveryProfile, DiscoveryRun, DiscoverySchedule
from app.services.discovery_ownership import recover_expired
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
            .order_by(DiscoverySchedule.profile_id, DiscoverySchedule.id)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    triggered: list[str] = []
    for s in rows:
        # Manual starts use the same profile row lock so a schedule and a user
        # cannot both pass the in-flight check for the same profile.
        profile = (await db.execute(select(DiscoveryProfile).where(
            DiscoveryProfile.id == s.profile_id
        ).with_for_update())).scalar_one_or_none()
        if profile is None:
            continue
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
    await db.commit()
    for run_id in triggered:
        start_run_task(uuid.UUID(run_id))
    if triggered:
        logger.info("scheduler tick: started %d run(s): %s", len(triggered), triggered)
    return {"triggered": triggered, "checked": len(rows)}


async def recover_stuck_runs(db: AsyncSession) -> int:
    """Fail only expired owners; committed queued work remains executable."""
    n = await recover_expired(db)
    if n:
        logger.warning("recovered %d discovery run(s) with expired owners", n)
    return n


async def resume_queued_runs(db: AsyncSession) -> int:
    ids = (await db.execute(select(DiscoveryRun.id).where(
        DiscoveryRun.status == 'queued', DiscoveryRun.executor_id.is_(None)
    ).order_by(DiscoveryRun.created_at).limit(100))).scalars().all()
    await db.commit()
    for run_id in ids:
        start_run_task(run_id)
    return len(ids)


async def discovery_scheduler_loop() -> None:
    """Recover expired work and schedule committed runs every minute."""
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(15)  # let the app settle before touching the DB
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await recover_stuck_runs(db)
                await resume_queued_runs(db)
                await run_scheduler_tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("discovery scheduler tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)
