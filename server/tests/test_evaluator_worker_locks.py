"""One evaluator pass at a time across API workers (NOTIFY 03).

Production runs two uvicorn workers and each starts every background loop.
The network, host, APM and APM-SLO evaluators and the escalation sweeper now
take a transaction-scoped advisory try-lock on a dedicated session before a
pass; the worker that does not get it skips that tick instead of evaluating
(and notifying) the same incidents a second time.

Runs on a disposable PostgreSQL; the evaluators themselves are faked.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_worker_lock as wl

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

PASSES = [
    ("app.services.network_alert_service", "run_network_alert_pass", "evaluate_network_rules",
     wl.NETWORK_ALERT_PASS),
    ("app.services.host_alert_service", "run_host_alert_pass", "evaluate_host_rules",
     wl.HOST_ALERT_PASS),
    ("app.services.apm_alert_service", "run_apm_alert_pass", "evaluate_apm_rules",
     wl.APM_ALERT_PASS),
    ("app.services.apm_slo_service", "run_slo_burn_pass", "evaluate_slo_burn", wl.APM_SLO_PASS),
    ("app.services.alert_escalation_service", "run_escalation_pass", "sweep_escalations",
     wl.ESCALATION_PASS),
]
IDS = [p[3].split(":", 1)[1] for p in PASSES]


@pytest.fixture
def sessions(pg_db):
    # NullPool: no connection outlives its session, so nothing to dispose.
    return async_sessionmaker(create_async_engine(pg_db.url, poolclass=NullPool),
                              expire_on_commit=False)


def fake_pass(monkeypatch, module_name, evaluator, delay=0.0):
    module = importlib.import_module(module_name)
    calls = []

    async def work(db):
        calls.append(db)
        await asyncio.sleep(delay)
        return {"ran": True}

    monkeypatch.setattr(module, evaluator, work)
    return module, calls


def test_every_pass_has_its_own_hashed_lock_name():
    names = [p[3] for p in PASSES]
    assert len(set(names)) == len(names)
    assert all(n.startswith("zenplus:") and n.endswith("_pass") for n in names)


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name,runner,evaluator,lock", PASSES, ids=IDS)
async def test_a_second_worker_skips_the_pass(sessions, monkeypatch, module_name, runner,
                                              evaluator, lock):
    module, calls = fake_pass(monkeypatch, module_name, evaluator)
    async with sessions() as other_worker:
        held = (await other_worker.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": lock})).scalar()
        assert held is True
        assert await getattr(module, runner)(sessions) is None     # skipped
        assert calls == []
        await other_worker.rollback()                               # that worker's pass ends
    assert await getattr(module, runner)(sessions) == {"ran": True}
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name,runner,evaluator,lock", PASSES, ids=IDS)
async def test_concurrent_workers_run_one_pass(sessions, monkeypatch, module_name, runner,
                                               evaluator, lock):
    module, calls = fake_pass(monkeypatch, module_name, evaluator, delay=0.3)
    run = getattr(module, runner)
    results = await asyncio.wait_for(asyncio.gather(run(sessions), run(sessions)), 10)
    assert sorted(results, key=bool) == [None, {"ran": True}] and len(calls) == 1
    # The lock ended with the pass: the next tick runs again.
    assert await run(sessions) == {"ran": True}


@pytest.mark.asyncio
async def test_a_failed_pass_releases_the_lock(sessions, monkeypatch):
    from app.services import network_alert_service as worker

    async def boom(db):
        raise RuntimeError("clickhouse unavailable")

    monkeypatch.setattr(worker, "evaluate_network_rules", boom)
    with pytest.raises(RuntimeError):
        await worker.run_network_alert_pass(sessions)
    async with wl.pass_lock(wl.NETWORK_ALERT_PASS, sessions) as held:
        assert held is True


@pytest.mark.asyncio
async def test_different_passes_do_not_block_each_other(sessions):
    async with wl.pass_lock(wl.NETWORK_ALERT_PASS, sessions) as network:
        async with wl.pass_lock(wl.HOST_ALERT_PASS, sessions) as host:
            assert network is True and host is True
