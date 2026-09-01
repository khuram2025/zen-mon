"""Remote-sensor lifecycle sweeping and alert-storm suppression."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.sensor_health")

SWEEP_INTERVAL_S = 30
OUTBOX_BATCH_LIMIT = 500
OUTBOX_IDLE_SLEEP_S = 0.5
SENSOR_SWEEP_LOCK_ID = 1_057_221
SENSOR_BOOTSTRAP_DIR = Path(
    os.getenv("ZENPLUS_SENSOR_BOOTSTRAP_DIR", "/opt/zenplus/artifacts/sensors/bootstrap")
)


def _purge_bootstrap_artifacts(sensor_id: Any) -> None:
    target = SENSOR_BOOTSTRAP_DIR / str(sensor_id)
    if target.parent.resolve() != SENSOR_BOOTSTRAP_DIR.resolve():
        raise RuntimeError("refusing to purge outside sensor bootstrap directory")
    if target.exists():
        shutil.rmtree(target)


async def create_sensor_alert(
    db: AsyncSession,
    *,
    sensor_id: str,
    severity: str,
    message: str,
    source: str,
) -> bool:
    existing = (await db.execute(
        text("""SELECT 1 FROM alerts
                 WHERE sensor_id = :sid
                   AND status IN ('pending', 'active', 'acknowledged')
                   AND metadata->>'dedupe' = :dedupe
                 LIMIT 1"""),
        {"sid": sensor_id, "dedupe": source},
    )).first()
    if existing:
        return False
    result = await db.execute(
        text("""INSERT INTO alerts (sensor_id, status, severity, message, metadata)
                VALUES (:sid, 'active', :severity, :message, CAST(:metadata AS jsonb))
                ON CONFLICT DO NOTHING"""),
        {
            "sid": sensor_id,
            "severity": severity,
            "message": message,
            "metadata": json.dumps({
                "source": source,
                "dedupe": source,
                "sensor_id": str(sensor_id),
            }),
        },
    )
    return bool(result.rowcount)


async def resolve_sensor_alert(db: AsyncSession, sensor_id: str, source: str, reason: str) -> int:
    result = await db.execute(
        text("""UPDATE alerts
                   SET status = 'resolved', resolved_at = NOW(),
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || CAST(:resolution AS jsonb)
                 WHERE sensor_id = :sid
                   AND status IN ('pending', 'active', 'acknowledged')
                   AND metadata->>'dedupe' = :dedupe"""),
        {
            "sid": sensor_id,
            "dedupe": source,
            "resolution": json.dumps({"resolved_by": "sensor_health", "reason": reason}),
        },
    )
    return result.rowcount or 0


async def _record_transitions(
    db: AsyncSession,
    rows: list[Any],
    *,
    new_status: str,
    reason: str,
) -> None:
    if not rows:
        return
    await db.execute(
        text("""INSERT INTO sensor_events (sensor_id, kind, detail)
                VALUES (:sensor_id, 'status_changed', CAST(:detail AS jsonb))"""),
        [
            {
                "sensor_id": row.id,
                "detail": json.dumps({
                    "from": row.old_status,
                    "to": new_status,
                    "reason": reason,
                    "last_heartbeat_at": row.last_heartbeat_at.isoformat()
                    if row.last_heartbeat_at else None,
                }),
            }
            for row in rows
        ],
    )


async def _suppress_owned_device_alerts(db: AsyncSession, sensor_id: str, sensor_name: str) -> int:
    """Invalidate stale device state before closing its now-unverifiable alerts.

    Resetting the cached state to ``unknown`` is important: when the sensor
    resumes and the target is still down, the first fresh result becomes an
    unknown -> down transition and the alert engine can open a new alert.
    """
    result = await db.execute(
        text("""WITH owned AS MATERIALIZED (
                    SELECT device_id FROM device_polling_owner
                     WHERE owner_kind = 'sensor' AND sensor_id = :sid
                ), invalidated AS (
                    UPDATE devices
                       SET status = 'unknown', updated_at = NOW()
                     WHERE id IN (SELECT device_id FROM owned)
                       AND status <> 'unknown'
                    RETURNING id
                )
                UPDATE alerts
                   SET status = 'resolved', resolved_at = NOW(),
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || CAST(:metadata AS jsonb)
                 WHERE status IN ('pending', 'active', 'acknowledged')
                   AND service_check_id IS NULL
                   AND COALESCE(metadata->>'new_status', '') IN ('down', 'degraded')
                   AND device_id IN (SELECT device_id FROM owned)"""),
        {
            "sid": sensor_id,
            "metadata": json.dumps({
                "suppressed_by": "sensor_offline",
                "sensor_id": str(sensor_id),
                "sensor_name": sensor_name,
                "resolved_to_status": "unknown",
            }),
        },
    )
    return result.rowcount or 0


async def _suppress_unverifiable_service_alerts(
    db: AsyncSession, sensor_id: str, sensor_name: str
) -> int:
    """Reset sole-vantage service checks so recovery can be re-evaluated."""
    result = await db.execute(
        text("""WITH assigned AS MATERIALIZED (
                    SELECT sc.id
                      FROM service_checks sc
                     WHERE sc.credential_id IS NULL
                       AND jsonb_array_length(
                             COALESCE(sc.workflow_steps, '[]'::jsonb)
                           ) = 0
                       AND (sc.default_sensor_id = :sid
                        OR sc.id IN (
                            SELECT a.target_id FROM sensor_assignments a
                             WHERE a.sensor_id = :sid
                               AND a.target_type = 'service_check'
                        ))
                ), unverifiable AS MATERIALIZED (
                    SELECT sc.id
                      FROM service_checks sc
                      JOIN assigned a ON a.id = sc.id
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM service_check_vantage_status v
                           LEFT JOIN sensors s ON s.id::text = v.poller_id
                          WHERE v.service_check_id = sc.id
                            AND (v.poller_id = 'central'
                                 OR s.status IN ('online', 'degraded'))
                            AND v.last_result_at >= NOW() - make_interval(
                                  secs => GREATEST(COALESCE(sc.check_interval, 60) * 2, 60)
                                )
                     )
                ), invalidated AS (
                    UPDATE service_checks
                       SET status = 'unknown', updated_at = NOW()
                     WHERE id IN (SELECT id FROM unverifiable)
                       AND status <> 'unknown'
                    RETURNING id
                )
                UPDATE alerts
                   SET status = 'resolved', resolved_at = NOW(),
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || CAST(:metadata AS jsonb)
                 WHERE status IN ('pending', 'active', 'acknowledged')
                   AND service_check_id IN (SELECT id FROM unverifiable)"""),
        {
            "sid": sensor_id,
            "metadata": json.dumps({
                "suppressed_by": "sensor_offline",
                "sensor_id": str(sensor_id),
                "sensor_name": sensor_name,
                "resolved_to_status": "unknown",
            }),
        },
    )
    return result.rowcount or 0


async def sweep_sensor_health(db: AsyncSession) -> dict[str, int]:
    """Advance online sensors to degraded/offline exactly once per transition."""
    locked = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": SENSOR_SWEEP_LOCK_ID},
    )).scalar_one()
    if not locked:
        return {"degraded": 0, "offline": 0, "alerts_suppressed": 0}

    offline = (await db.execute(text("""
        WITH candidates AS (
            SELECT id, name, status AS old_status, last_heartbeat_at
              FROM sensors
             WHERE status IN ('online', 'degraded')
               AND last_heartbeat_at IS NOT NULL
               AND last_heartbeat_at <= NOW() - make_interval(secs => offline_after_s)
             FOR UPDATE
        )
        UPDATE sensors s
           SET status = 'offline',
               status_reason = 'Heartbeat overdue beyond offline threshold',
               updated_at = NOW()
          FROM candidates c
         WHERE s.id = c.id
        RETURNING s.id, s.name, c.old_status, c.last_heartbeat_at
    """))).fetchall()

    degraded = (await db.execute(text("""
        WITH candidates AS (
            SELECT id, name, status AS old_status, last_heartbeat_at
              FROM sensors
             WHERE status = 'online'
               AND last_heartbeat_at IS NOT NULL
               AND last_heartbeat_at <= NOW() - make_interval(secs => degraded_after_s)
               AND last_heartbeat_at > NOW() - make_interval(secs => offline_after_s)
             FOR UPDATE
        )
        UPDATE sensors s
           SET status = 'degraded',
               status_reason = 'Heartbeat overdue beyond degraded threshold',
               updated_at = NOW()
          FROM candidates c
         WHERE s.id = c.id
        RETURNING s.id, s.name, c.old_status, c.last_heartbeat_at
    """))).fetchall()

    await _record_transitions(
        db, offline, new_status="offline", reason="heartbeat timeout"
    )
    await _record_transitions(
        db, degraded, new_status="degraded", reason="late heartbeat"
    )

    suppressed = 0
    for row in degraded:
        await resolve_sensor_alert(db, str(row.id), "sensor_offline", "sensor is degraded")
        await create_sensor_alert(
            db,
            sensor_id=str(row.id),
            severity="warning",
            message=f"Remote sensor {row.name} is degraded — heartbeat is late",
            source="sensor_degraded",
        )
    for row in offline:
        await resolve_sensor_alert(db, str(row.id), "sensor_degraded", "sensor is offline")
        await create_sensor_alert(
            db,
            sensor_id=str(row.id),
            severity="critical",
            message=f"Remote sensor {row.name} is offline — assigned targets may be stale",
            source="sensor_offline",
        )
        suppressed += await _suppress_owned_device_alerts(db, str(row.id), row.name)
        suppressed += await _suppress_unverifiable_service_alerts(
            db, str(row.id), row.name
        )

    expired_artifact_ids = (await db.execute(text("""
        SELECT id FROM sensors
         WHERE enrollment_consumed_at IS NOT NULL
            OR enrollment_expires_at < NOW()
    """))).scalars().all()
    await db.execute(text("DELETE FROM sensor_events WHERE ts < NOW() - INTERVAL '30 days'"))
    await db.execute(text("DELETE FROM sensor_ingest_ledger WHERE created_at < NOW() - INTERVAL '7 days'"))
    await db.execute(text("""DELETE FROM sensor_transition_outbox
                              WHERE processed_at < NOW() - INTERVAL '7 days'"""))
    await db.commit()
    for sensor_id in expired_artifact_ids:
        try:
            await asyncio.to_thread(_purge_bootstrap_artifacts, sensor_id)
        except Exception:
            logger.exception("failed to purge expired bootstrap artifacts for %s", sensor_id)

    if offline or degraded:
        logger.info(
            "sensor sweep: %d degraded, %d offline, %d dependent alerts suppressed",
            len(degraded), len(offline), suppressed,
        )
    return {
        "degraded": len(degraded),
        "offline": len(offline),
        "alerts_suppressed": suppressed,
    }


def _outbox_timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return value


async def dispatch_sensor_transition_outbox(
    db: AsyncSession, *, limit: int = OUTBOX_BATCH_LIMIT
) -> dict[str, int]:
    """Deliver durable sensor transitions with retry/backoff.

    ``processed_at`` is set before calling the alert engine; the alert
    engine's own commit then atomically persists its alert rows and the
    outbox acknowledgement. A failure rolls both back for a later retry.
    """
    from app.api.v1.sensor_api import (
        _dispatch_device_transitions,
        _dispatch_service_transitions,
    )

    processed = 0
    failed = 0
    for _ in range(max(1, min(limit, 500))):
        row = (await db.execute(text("""
            SELECT pending.id, pending.transition_type, pending.payload
              FROM sensor_transition_outbox AS pending
             WHERE pending.processed_at IS NULL
               AND pending.next_attempt_at <= NOW()
               AND NOT EXISTS (
                    SELECT 1
                      FROM sensor_transition_outbox AS older
                     WHERE older.processed_at IS NULL
                       AND older.transition_type = pending.transition_type
                       AND older.entity_id = pending.entity_id
                       AND (older.created_at, older.id)
                           < (pending.created_at, pending.id)
               )
             ORDER BY pending.created_at, pending.id
             FOR UPDATE OF pending SKIP LOCKED
             LIMIT 1
        """))).mappings().first()
        if not row:
            break

        payload = dict(row["payload"] or {})
        payload["timestamp"] = _outbox_timestamp(payload.get("timestamp"))
        try:
            await db.execute(text("""UPDATE sensor_transition_outbox
                                         SET processed_at = NOW(), last_error = NULL
                                       WHERE id = :id"""), {"id": row["id"]})
            if row["transition_type"] == "device":
                await _dispatch_device_transitions(
                    [payload], db, raise_on_error=True
                )
            else:
                await _dispatch_service_transitions(
                    [payload], db, raise_on_error=True
                )
            await db.commit()
            processed += 1
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            await db.execute(text("""UPDATE sensor_transition_outbox
                                         SET attempts = attempts + 1,
                                             last_error = :error,
                                             next_attempt_at = NOW() + make_interval(
                                                 secs => LEAST(300, (2 ^ LEAST(attempts, 8)))::integer
                                             )
                                       WHERE id = :id AND processed_at IS NULL"""),
                             {"id": row["id"], "error": str(exc)[:2048]})
            await db.commit()
            failed += 1
    return {"processed": processed, "failed": failed}


async def sensor_health_sweeper_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(10)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep_sensor_health(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sensor health sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)


async def sensor_transition_outbox_loop() -> None:
    """Continuously drain durable status transitions.

    Every API worker may run this loop. ``SKIP LOCKED`` distributes unrelated
    entities, while the anti-join in ``dispatch_sensor_transition_outbox``
    prevents a newer transition from passing an older unprocessed transition
    for the same entity (including while the older row is in retry backoff).
    """
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(2)
    while True:
        processed = 0
        try:
            async with AsyncSessionLocal() as db:
                result = await dispatch_sensor_transition_outbox(db)
                processed = result["processed"]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sensor transition outbox dispatch failed")
        # Immediately take the next bounded batch after a burst. Otherwise use
        # a short idle pause so newly ingested transitions do not wait for the
        # 30-second lifecycle sweep.
        if processed < OUTBOX_BATCH_LIMIT:
            await asyncio.sleep(OUTBOX_IDLE_SLEEP_S)
