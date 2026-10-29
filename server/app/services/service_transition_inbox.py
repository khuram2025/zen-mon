"""Consume confirmed central-service transitions in order with atomic effects.

The poller saves state + work in one transaction. This worker commits incident
changes, notification intents and the consumed marker in another transaction.
Neither boundary depends on a live loopback HTTP request.
"""
import asyncio
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


async def consume_one(session_factory):
    from app.api.v1.alert_engine import ServiceStatusChangeEvent, evaluate_service_transition
    async with session_factory() as db:
        row = (await db.execute(text('''
            SELECT q.id,q.payload,q.observed_at FROM service_transition_inbox q
            WHERE q.processed_at IS NULL AND q.due_at<=clock_timestamp()
              AND NOT EXISTS (SELECT 1 FROM service_transition_inbox earlier
                  WHERE earlier.service_check_id=q.service_check_id
                    AND earlier.processed_at IS NULL AND earlier.id<q.id)
            ORDER BY q.id FOR UPDATE OF q SKIP LOCKED LIMIT 1
        '''))).mappings().first()
        if row is None:
            return False
        try:
            # A savepoint keeps the claim locked if evaluation fails; the
            # retry schedule must not race another consumer after rollback.
            async with db.begin_nested():
                event = ServiceStatusChangeEvent.model_validate(row['payload'])
                await evaluate_service_transition(event, db, commit=False, observed_at=row['observed_at'])
                await db.execute(text('''
                    UPDATE service_transition_inbox SET processed_at=clock_timestamp(),
                    attempts=attempts+1,last_error_type=NULL WHERE id=:id
                '''), {'id': row['id']})
        except Exception as exc:
            await db.execute(text('''
                UPDATE service_transition_inbox SET attempts=attempts+1,
                    due_at=clock_timestamp()+interval '10 seconds',last_error_type=:error
                WHERE id=:id
            '''), {'id': row['id'], 'error': type(exc).__name__})
            logger.warning('Service transition %s deferred (%s)', row['id'], type(exc).__name__)
        await db.commit()
        return True


async def service_transition_loop():
    from app.core.database import AsyncSessionLocal
    from app.core.controller_admission import permitted
    while True:
        try:
            for _ in range(32):
                if not permitted() or not await consume_one(AsyncSessionLocal):
                    break
            if permitted():
                async with AsyncSessionLocal() as db:
                    await db.execute(text('''
                        DELETE FROM service_transition_inbox WHERE id IN (
                            SELECT id FROM service_transition_inbox
                            WHERE processed_at<clock_timestamp()-interval '7 days'
                            ORDER BY processed_at LIMIT 1000)
                    '''))
                    await db.commit()
        except Exception as exc:
            logger.warning('Service transition consumer unavailable (%s)', type(exc).__name__)
        await asyncio.sleep(2)
