"""Database ownership for read-only discovery, not external executor fencing.

A unique execution token owns a run. Expired running work fails visibly; queued
work can be claimed after a process restart. Every result transaction locks and
checks the run before writing and checks expiry again before committing. Never
use this lease as permission for configuration changes on remote devices.
"""
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from app.models.discovery_v2 import DiscoveryRun

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 15


class OwnershipLost(Exception):
    pass


async def claim(sessions, run_id, token):
    async with sessions() as db:
        result = await db.execute(text("""
            UPDATE discovery_runs SET status='running', executor_id=:token,
                heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + :ttl * interval '1 second'
            WHERE id=:id AND status='queued' AND executor_id IS NULL
            RETURNING id
        """), {'id': run_id, 'token': token, 'ttl': LEASE_SECONDS})
        owned = result.scalar_one_or_none() is not None
        await db.commit()
        return owned


async def renew(sessions, run_id, token):
    async with sessions() as db:
        result = await db.execute(text("""
            UPDATE discovery_runs SET heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + :ttl * interval '1 second'
            WHERE id=:id AND executor_id=:token AND status='running'
                AND lease_expires_at > clock_timestamp()
            RETURNING id
        """), {'id': run_id, 'token': token, 'ttl': LEASE_SECONDS})
        owned = result.scalar_one_or_none() is not None
        await db.commit()
        return owned


@asynccontextmanager
async def owned_session(sessions, run_id, token):
    async with sessions() as db:
        run = (await db.execute(
            select(DiscoveryRun).where(
                DiscoveryRun.id == run_id,
                DiscoveryRun.executor_id == token,
                DiscoveryRun.status == 'running',
                DiscoveryRun.lease_expires_at > text('clock_timestamp()'),
            ).with_for_update()
        )).scalar_one_or_none()
        if run is None:
            raise OwnershipLost('Discovery run ownership expired or was revoked')
        yield db
        # The row lock serializes cancellation/recovery against all result writes.
        # Flush first so a failed final check rolls back results and ORM updates.
        await db.flush()
        result = await db.execute(text("""
            UPDATE discovery_runs SET heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + :ttl * interval '1 second'
            WHERE id=:id AND executor_id=:token
                AND lease_expires_at > clock_timestamp() RETURNING id
        """), {'id': run_id, 'token': token, 'ttl': LEASE_SECONDS})
        if result.scalar_one_or_none() is None:
            raise OwnershipLost('Discovery run lease expired during transaction')
        await db.commit()


async def finish(sessions, run_id, token, status, error=None):
    """Best-effort terminal state; never overwrite cancellation or a new owner."""
    async with sessions() as db:
        await db.execute(text("""
            UPDATE discovery_runs SET status=:status, phase='done',
                completed_at=clock_timestamp(), error_details=:error
            WHERE id=:id AND executor_id=:token AND status='running'
                AND lease_expires_at > clock_timestamp()
        """), {'id': run_id, 'token': token, 'status': status, 'error': error})
        await db.commit()


async def recover_expired(db):
    # Unowned legacy running rows are recoverable only after all old executors
    # have been stopped for the coordinated upgrade. Queued rows are retained.
    result = await db.execute(text("""
        UPDATE discovery_runs SET status='failed', phase='done',
            completed_at=clock_timestamp(),
            error_details='Discovery executor ownership expired; rerun required'
        WHERE status='running' AND
            (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
    """))
    await db.commit()
    return result.rowcount or 0
