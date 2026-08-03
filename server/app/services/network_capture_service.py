"""Lifecycle reconciliation for bounded, on-demand network captures.

The API and agent communicate over an at-least-once command channel.  These
helpers keep the Postgres control row honest when a command expires, fails,
or an agent disappears before its final upload.  A transaction-scoped
advisory lock makes the periodic sweep safe with multiple Uvicorn workers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.network_capture")

SWEEP_INTERVAL_S = 30
CAPTURE_COMMAND_TTL_S = 120
CAPTURE_STOP_COMMAND_TTL_S = 90
CAPTURE_REAP_GRACE_S = 45
CAPTURE_RETENTION_S = 60 * 60
CAPTURE_RETENTION_MIN_S = 15 * 60
CAPTURE_RETENTION_MAX_S = 7 * 24 * 60 * 60
CAPTURE_PURGE_BATCH_SIZE = 100

ACTIVE_CAPTURE_STATUSES = frozenset({"queued", "running", "stopping"})
TERMINAL_CAPTURE_STATUSES = frozenset(
    {"completed", "failed", "expired", "cancelled"}
)

# Stable, application-specific signed 32-bit key ("ZNCP").  The lock is held
# only for the current transaction and therefore releases on commit/rollback
# or if a worker dies.
CAPTURE_SWEEP_ADVISORY_LOCK = 1515074384
CAPTURE_PURGE_ADVISORY_LOCK = 1515074385


def delete_capture_clickhouse_data(capture_id: Any) -> None:
    """Synchronously remove both ClickHouse datasets for one capture.

    Mutations are forced synchronous so callers only delete the PostgreSQL
    control row after ClickHouse confirms both removals. Repeating either
    mutation is safe, which makes a partial failure retryable.
    """
    from app.core.database import get_ch_client

    normalized_id = str(UUID(str(capture_id)))
    client = get_ch_client()
    for table in (
        "zenplus.host_network_flows",
        "zenplus.host_network_traffic_samples",
    ):
        client.command(
            f"ALTER TABLE {table} DELETE "
            f"WHERE capture_id = toUUID('{normalized_id}') "
            "SETTINGS mutations_sync = 1"
        )


def _capture_id(params: Any) -> Optional[str]:
    if not isinstance(params, dict):
        return None
    value = str(params.get("capture_id") or "").strip()
    return value or None


async def reconcile_capture_command_result(
    db: AsyncSession,
    *,
    command: str,
    params: Any,
    success: bool,
    error_message: Optional[str],
) -> None:
    """Reflect terminal start/stop command results on the capture row."""
    capture_id = _capture_id(params)
    if not capture_id or command not in ("start_network_capture", "stop_network_capture"):
        return

    if command == "stop_network_capture" and success:
        await db.execute(
            text("""UPDATE network_captures SET
                      status = 'cancelled',
                      completed_at = COALESCE(completed_at, NOW()),
                      note = COALESCE(NULLIF(note, ''), 'Capture stopped by operator.'),
                      updated_at = NOW()
                    WHERE id = :id AND status = 'stopping'"""),
            {"id": capture_id},
        )
        return

    if success:
        # Start success only means the command was accepted.  The first upload
        # advances queued -> running and carries the authoritative timestamps.
        return

    action = "start" if command == "start_network_capture" else "stop"
    message = (error_message or f"Agent failed to {action} the network capture.")[:4000]
    active_states = "('queued','running')" if action == "start" else "('stopping')"
    await db.execute(
        text(f"""UPDATE network_captures SET
                  status = 'failed',
                  completed_at = COALESCE(completed_at, NOW()),
                  error_message = :error,
                  updated_at = NOW()
                WHERE id = :id AND status IN {active_states}"""),
        {"id": capture_id, "error": message},
    )


async def expire_agent_commands(
    db: AsyncSession,
    *,
    agent_id: Any = None,
) -> int:
    """Expire overdue commands and reconcile affected capture rows.

    This does not commit; callers can combine expiration with polling or the
    periodic lifecycle sweep in one transaction.
    """
    agent_filter = " AND agent_id = :agent_id" if agent_id is not None else ""
    params = {"agent_id": agent_id} if agent_id is not None else {}
    expired = (await db.execute(
        text(f"""UPDATE agent_commands SET
                  status = 'expired', completed_at = COALESCE(completed_at, NOW())
                WHERE status IN ('queued','sent')
                  AND expires_at IS NOT NULL AND expires_at <= NOW()
                  {agent_filter}
                RETURNING command, params"""),
        params,
    )).mappings().all()

    for row in expired:
        capture_id = _capture_id(row.get("params"))
        if not capture_id:
            continue
        if row["command"] == "start_network_capture":
            await db.execute(
                text("""UPDATE network_captures SET
                          status = 'failed', completed_at = NOW(),
                          error_message = COALESCE(NULLIF(error_message, ''),
                            'The agent did not pick up the capture command before it expired.'),
                          updated_at = NOW()
                        WHERE id = :id AND status = 'queued'"""),
                {"id": capture_id},
            )
        elif row["command"] == "stop_network_capture":
            await db.execute(
                text("""UPDATE network_captures SET
                          status = 'cancelled', completed_at = COALESCE(completed_at, NOW()),
                          note = COALESCE(NULLIF(note, ''),
                            'The stop command was not acknowledged; the bounded agent capture '
                            'may have continued until its original end time.'),
                          updated_at = NOW()
                        WHERE id = :id AND status = 'stopping'"""),
                {"id": capture_id},
            )
    return len(expired)


async def sweep_stale_captures(db: AsyncSession) -> dict[str, int]:
    acquired = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": CAPTURE_SWEEP_ADVISORY_LOCK},
    )).first()
    if not acquired or not acquired[0]:
        await db.rollback()
        return {"completed": 0, "failed": 0, "cancelled": 0, "expired_commands": 0}

    expired_commands = await expire_agent_commands(db)

    finished = await db.execute(
        text("""UPDATE network_captures SET
                  status = 'completed',
                  completed_at = COALESCE(completed_at, NOW()),
                  note = COALESCE(NULLIF(note, ''),
                                  'Agent stopped reporting before it confirmed the end '
                                  'of the capture; flows collected up to that point are shown.'),
                  updated_at = NOW()
                WHERE status = 'running'
                  AND ends_at IS NOT NULL
                  AND ends_at < NOW() - make_interval(secs => :grace)
                RETURNING id"""),
        {"grace": CAPTURE_REAP_GRACE_S},
    )
    finished_n = len(finished.fetchall())

    orphaned = await db.execute(
        text("""UPDATE network_captures SET
                  status = 'completed',
                  completed_at = COALESCE(completed_at, NOW()),
                  note = COALESCE(NULLIF(note, ''),
                                  'Agent stopped reporting before it confirmed the end '
                                  'of the capture; flows collected up to that point are shown.'),
                  updated_at = NOW()
                WHERE status = 'running'
                  AND ends_at IS NULL
                  AND COALESCE(started_at, requested_at)
                      < NOW() - make_interval(secs => duration_s + :grace)
                RETURNING id"""),
        {"grace": CAPTURE_REAP_GRACE_S},
    )
    orphaned_n = len(orphaned.fetchall())

    never_started = await db.execute(
        text("""UPDATE network_captures SET
                  status = 'failed', completed_at = NOW(),
                  error_message = COALESCE(NULLIF(error_message, ''),
                    'The agent did not pick up the capture in time. Verify that it is online '
                    'and advertises network_capture_v1.'),
                  updated_at = NOW()
                WHERE status = 'queued'
                  AND requested_at < NOW() - make_interval(secs => :ttl)
                RETURNING id"""),
        {"ttl": CAPTURE_COMMAND_TTL_S},
    )
    never_started_n = len(never_started.fetchall())

    abandoned_stops = await db.execute(
        text("""UPDATE network_captures SET
                  status = 'cancelled', completed_at = COALESCE(completed_at, NOW()),
                  note = COALESCE(NULLIF(note, ''),
                    'The stop request was not acknowledged; traffic collected before the '
                    'request is retained.'),
                  updated_at = NOW()
                WHERE status = 'stopping'
                  AND updated_at < NOW() - make_interval(secs => :ttl)
                RETURNING id"""),
        {"ttl": CAPTURE_STOP_COMMAND_TTL_S + CAPTURE_REAP_GRACE_S},
    )
    cancelled_n = len(abandoned_stops.fetchall())

    await db.commit()
    if finished_n or orphaned_n or never_started_n or cancelled_n or expired_commands:
        logger.info(
            "capture sweep: %d ended, %d abandoned, %d never started, "
            "%d stops abandoned, %d commands expired",
            finished_n, orphaned_n, never_started_n, cancelled_n, expired_commands,
        )
    return {
        "completed": finished_n + orphaned_n,
        "failed": never_started_n,
        "cancelled": cancelled_n,
        "expired_commands": expired_commands,
    }


async def purge_expired_captures(
    db: AsyncSession,
    *,
    limit: int = CAPTURE_PURGE_BATCH_SIZE,
) -> dict[str, int]:
    """Purge due, unarchived terminal captures from both data stores.

    Rows are locked while their ClickHouse mutations run. An archive request
    that won the row lock first excludes the capture; one that arrives after
    cleanup started waits and observes the deleted row. PostgreSQL metadata is
    retained whenever either ClickHouse mutation fails so the next sweep can
    retry without orphaning flow data.
    """
    acquired = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": CAPTURE_PURGE_ADVISORY_LOCK},
    )).first()
    if not acquired or not acquired[0]:
        await db.rollback()
        return {"purged": 0, "failed": 0}

    batch_limit = max(1, min(int(limit), 1000))
    candidates = (await db.execute(
        text("""SELECT id
                FROM network_captures
                WHERE status IN ('completed','failed','expired','cancelled')
                  AND archived_at IS NULL
                  AND purge_after IS NOT NULL
                  AND purge_after <= NOW()
                ORDER BY purge_after, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED"""),
        {"limit": batch_limit},
    )).fetchall()

    purged = 0
    failed = 0
    for row in candidates:
        capture_id = row[0]
        try:
            await asyncio.to_thread(delete_capture_clickhouse_data, capture_id)
        except Exception:
            failed += 1
            logger.exception(
                "capture purge retained metadata after ClickHouse failure: id=%s",
                capture_id,
            )
            # A mutation failure is normally store-wide. Avoid hammering an
            # unavailable ClickHouse once per row; the next sweep retries the
            # same safely retained metadata.
            break

        deleted = await db.execute(
            text("""DELETE FROM network_captures
                    WHERE id = :id
                      AND status IN ('completed','failed','expired','cancelled')
                      AND archived_at IS NULL
                      AND purge_after IS NOT NULL
                      AND purge_after <= NOW()
                    RETURNING id"""),
            {"id": capture_id},
        )
        purged += len(deleted.fetchall())

    await db.commit()
    if purged or failed:
        logger.info("capture retention purge: purged=%d failed=%d", purged, failed)
    return {"purged": purged, "failed": failed}


async def capture_sweeper_loop() -> None:
    """Background task: reconcile lifecycle and enforce capture retention."""
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(15)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep_stale_captures(db)
                await purge_expired_captures(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("network capture sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
