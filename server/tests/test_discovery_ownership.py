"""Competing workers, expired ownership and cancellation with private PostgreSQL."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.services import discovery_executor as executor
from app.services import discovery_ownership as owner
from app.services import discovery_scheduler as scheduler
from pg_fixture import pg_cluster, pg_db  # noqa: F401


def setup(pg_db, monkeypatch):
    sessions = async_sessionmaker(create_async_engine(pg_db.url, poolclass=NullPool),
                                 expire_on_commit=False)
    monkeypatch.setattr(executor, 'AsyncSessionLocal', sessions)
    profile, run = uuid4(), uuid4()
    pg_db.execute("INSERT INTO discovery_profiles(id,name,targets) VALUES ($1,$2,'[\"127.0.0.1\"]')",
                  profile, str(profile))
    pg_db.execute("INSERT INTO discovery_runs(id,profile_id,trigger_type,status) VALUES ($1,$2,'manual','queued')",
                  run, profile)
    return sessions, profile, run


@pytest.mark.asyncio
async def test_competing_claims_and_worker_startup_preserve_live_run(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    tokens = [uuid4(), uuid4()]
    results = await asyncio.gather(*(owner.claim(sessions, run, token) for token in tokens))
    assert sorted(results) == [False, True]
    winner = tokens[results.index(True)]
    async with sessions() as db:
        assert await scheduler.recover_stuck_runs(db) == 0
    assert pg_db.scalar('SELECT executor_id FROM discovery_runs WHERE id=$1', run) == winner
    assert await owner.renew(sessions, run, winner)
    assert not await owner.renew(sessions, run, tokens[results.index(False)])


@pytest.mark.asyncio
async def test_expired_owner_cannot_renew_commit_or_overwrite_recovery(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    token = uuid4()
    assert await owner.claim(sessions, run, token)
    pg_db.execute("UPDATE discovery_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1", run)
    assert not await owner.renew(sessions, run, token)
    with pytest.raises(owner.OwnershipLost):
        async with owner.owned_session(sessions, run, token):
            pytest.fail('expired worker reached its write transaction')
    async with sessions() as db:
        assert await scheduler.recover_stuck_runs(db) == 1
    await owner.finish(sessions, run, token, 'completed')
    assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'failed'


@pytest.mark.asyncio
async def test_expiry_during_transaction_rolls_back_results(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    token = uuid4()
    assert await owner.claim(sessions, run, token)
    with pytest.raises(owner.OwnershipLost):
        async with owner.owned_session(sessions, run, token) as db:
            await db.execute(text("UPDATE discovery_runs SET completed_targets=99, lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {'id': run})
    assert pg_db.scalar('SELECT completed_targets FROM discovery_runs WHERE id=$1', run) == 0


@pytest.mark.asyncio
async def test_remote_cancellation_blocks_result_writes(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    token = uuid4()
    assert await owner.claim(sessions, run, token)
    pg_db.execute("UPDATE discovery_runs SET status='cancelled' WHERE id=$1", run)
    with pytest.raises(owner.OwnershipLost):
        async with owner.owned_session(sessions, run, token):
            pytest.fail('cancelled worker reached its write transaction')
    await owner.finish(sessions, run, token, 'failed')
    assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'cancelled'


@pytest.mark.asyncio
async def test_queued_work_survives_restart_and_runs_once(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    launched = []
    monkeypatch.setattr(scheduler, 'start_run_task', launched.append)
    async with sessions() as db:
        assert await scheduler.recover_stuck_runs(db) == 0
        assert await scheduler.resume_queued_runs(db) == 1
    assert launched == [run]
    async def probe(*args):
        return {'_responsive': False, '_probes': {}}
    monkeypatch.setattr(executor, '_probe_ip', probe)
    await asyncio.gather(executor.execute_run(run), executor.execute_run(run))
    assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'completed'
    assert pg_db.scalar('SELECT count(*) FROM discovery_results_v2 WHERE run_id=$1', run) == 1


@pytest.mark.asyncio
async def test_cancelling_executor_joins_child_probes(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def probe(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(executor, '_probe_ip', probe)
    task = asyncio.create_task(executor.execute_run(run))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'cancelled'
    assert pg_db.scalar('SELECT count(*) FROM discovery_results_v2 WHERE run_id=$1', run) == 0


@pytest.mark.asyncio
async def test_scheduled_launch_is_committed_and_one_per_profile(pg_db, monkeypatch):
    sessions, profile, initial = setup(pg_db, monkeypatch)
    pg_db.execute('DELETE FROM discovery_runs WHERE id=$1', initial)
    pg_db.execute("INSERT INTO discovery_schedules(profile_id,schedule_type,next_run_at) VALUES ($1,'once_now',clock_timestamp()-interval '1 second')", profile)
    launched = []
    def launch(run):
        # Another connection must see the row before an executor is dispatched.
        assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'queued'
        launched.append(run)
    monkeypatch.setattr(scheduler, 'start_run_task', launch)
    async def tick():
        async with sessions() as db:
            return await scheduler.run_scheduler_tick(db)
    await asyncio.wait_for(asyncio.gather(tick(), tick()), 10)
    assert len(launched) == 1
    assert pg_db.scalar('SELECT count(*) FROM discovery_runs WHERE profile_id=$1', profile) == 1


@pytest.mark.asyncio
async def test_heartbeat_revocation_stops_waiting_probe(pg_db, monkeypatch):
    sessions, _, run = setup(pg_db, monkeypatch)
    entered, stopped = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(owner, 'HEARTBEAT_SECONDS', .05)
    async def probe(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(executor, '_probe_ip', probe)
    task = asyncio.create_task(executor.execute_run(run))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pg_db.execute("UPDATE discovery_runs SET status='cancelled' WHERE id=$1", run)
        await asyncio.wait_for(task, 5)
        assert stopped.is_set()
        assert pg_db.scalar('SELECT count(*) FROM discovery_results_v2 WHERE run_id=$1', run) == 0
        assert pg_db.scalar('SELECT status FROM discovery_runs WHERE id=$1', run) == 'cancelled'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
