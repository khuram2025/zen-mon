"""Turn admitted traps into incidents and notification work atomically."""
import asyncio
import logging
import os

from sqlalchemy import text

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("TRAP_INCIDENT_INBOX_ENABLED") == "1"


async def consume_one(session_factory) -> bool:
    from app.api.v1.alert_engine import TrapEvent, evaluate_trap_event

    async with session_factory() as db:
        row = (await db.execute(text("""
            SELECT q.id,q.event_id,q.payload,q.payload_bytes,q.observed_at
            FROM snmp_trap_incident_inbox q
            WHERE q.due_at<=clock_timestamp()
              AND NOT EXISTS (SELECT 1 FROM snmp_trap_incident_inbox earlier
                  WHERE earlier.source_key=q.source_key AND earlier.id<q.id)
            ORDER BY q.id FOR UPDATE OF q SKIP LOCKED LIMIT 1
        """))).mappings().first()
        if row is None:
            return False
        try:
            async with db.begin_nested():
                event = TrapEvent.model_validate({**row["payload"], "event_id": row["event_id"]})
                await evaluate_trap_event(event, db, observed_at=row["observed_at"])
                # Match the producer's capacity-before-removal order. A
                # competing admission can then dedupe against the existing row.
                await db.execute(text("""
                    UPDATE host_ingest_capacity SET trap_bytes=trap_bytes-:size WHERE singleton
                """), {"size": row["payload_bytes"]})
                await db.execute(text("DELETE FROM snmp_trap_incident_inbox WHERE id=:id"), {"id": row["id"]})
        except Exception as exc:
            await db.execute(text("""
                UPDATE snmp_trap_incident_inbox SET attempts=attempts+1,
                    due_at=clock_timestamp()+interval '10 seconds',last_error_type=:error
                WHERE id=:id
            """), {"id": row["id"], "error": type(exc).__name__})
            logger.warning("Trap incident %s deferred (%s)", row["id"], type(exc).__name__)
        await db.commit()
        return True


async def trap_incident_loop():
    from app.core.controller_admission import permitted
    from app.core.database import AsyncSessionLocal

    while True:
        try:
            if permitted() and await consume_one(AsyncSessionLocal):
                continue
        except Exception as exc:
            logger.warning("Trap incident inbox unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(2)
