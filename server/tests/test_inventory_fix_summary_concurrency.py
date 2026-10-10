"""Regression tests of the Phase 1 review for the summary cache (F9, with F5's pool rule).

After the 60 s expiry every caller of GET /inventory/summary rebuilt the whole block on its own (20 concurrent cold callers ran
it 20 times). The cache now builds each key once and shares the result; a caller releases its database connection before it
waits, so the callers cannot use the pool up and starve the build they are waiting for.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import inventory_query as iq
from inventory_fleet import SCOPED_TAG, fleet_db_fresh, fleet_info, fleet_template, pinned_clock  # noqa: F401
from pg_fixture import pg_cluster  # noqa: F401  (fixture)


@pytest.fixture
def slow_core(monkeypatch):
    calls: list[tuple] = []
    real = iq._summary_core

    async def slow(db, scope, *a, **k):
        calls.append(tuple(scope) if scope is not None else None)
        await asyncio.sleep(0.25)
        return await real(db, scope, *a, **k)

    monkeypatch.setattr(iq, "_summary_core", slow)
    return calls


@pytest.mark.asyncio
async def test_f9_concurrent_cold_callers_share_one_build(fleet_db_fresh, pinned_clock, slow_core):
    engine = create_async_engine(fleet_db_fresh.url, poolclass=NullPool)
    try:
        async def one(scope):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await iq.summary(s, scope=scope)

        results = await asyncio.gather(*[one(None) for _ in range(12)], *[one([SCOPED_TAG]) for _ in range(5)])
    finally:
        await engine.dispose()
    assert len(slow_core) == 2 and set(slow_core) == {None, (SCOPED_TAG,)}, "one build per cache key, not one per caller"
    assert all(r is results[0] for r in results[:12]) and all(r is results[12] for r in results[12:])
    assert results[0]["assets"]["total"] > results[12]["assets"]["total"]
    assert iq._summary_inflight == {}, "nothing is left in flight"


@pytest.mark.asyncio
async def test_f9_the_next_caller_after_a_build_is_served_from_the_cache(fleet_db_fresh, pinned_clock, slow_core):
    engine = create_async_engine(fleet_db_fresh.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            first = await iq.summary(s, scope=None)
            again = await iq.summary(s, scope=None)
    finally:
        await engine.dispose()
    assert again is first and len(slow_core) == 1


@pytest.mark.asyncio
async def test_f9_callers_release_their_connection_while_they_wait(fleet_db_fresh, pinned_clock, slow_core):
    """F5's rule: on a pool of four, four callers that each held a connection while waiting left the build none (it needs up
    to three): every caller timed out. They hold none now."""
    engine = create_async_engine(fleet_db_fresh.url, pool_size=4, max_overflow=0, pool_timeout=5)
    try:
        async def one():
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await iq.summary(s, scope=None)

        results = await asyncio.wait_for(asyncio.gather(*[one() for _ in range(8)]), timeout=30)
    finally:
        await engine.dispose()
    assert len(slow_core) == 1 and all(r is results[0] for r in results)


@pytest.mark.asyncio
async def test_f9_a_caller_that_goes_away_does_not_cancel_the_shared_build(fleet_db_fresh, pinned_clock, slow_core):
    engine = create_async_engine(fleet_db_fresh.url, poolclass=NullPool)
    try:
        async def one():
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await iq.summary(s, scope=None)

        waiting = asyncio.create_task(one())
        await asyncio.sleep(0.08)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        for _ in range(300):                                         # the build finishes on its own (a loaded host takes longer)
            if any(k[0] is None for k in iq._summary_cache):
                break
            await asyncio.sleep(0.05)
        assert any(k[0] is None for k in iq._summary_cache)
        assert len(slow_core) == 1
        async with AsyncSession(engine, expire_on_commit=False) as s:
            await iq.summary(s, scope=None)
        assert len(slow_core) == 1, "served from what the abandoned build left in the cache"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_f9_a_failed_build_reaches_every_waiter_and_is_not_cached(fleet_db_fresh, pinned_clock, monkeypatch):
    real = iq._summary_core
    state = {"fail": True, "calls": 0}

    async def flaky(db, scope, *a, **k):
        state["calls"] += 1
        await asyncio.sleep(0.1)
        if state["fail"]:
            raise RuntimeError("boom")
        return await real(db, scope, *a, **k)

    monkeypatch.setattr(iq, "_summary_core", flaky)
    engine = create_async_engine(fleet_db_fresh.url, poolclass=NullPool)
    try:
        async def one():
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await iq.summary(s, scope=None)

        outcomes = await asyncio.gather(*[one() for _ in range(4)], return_exceptions=True)
        assert all(isinstance(o, RuntimeError) for o in outcomes) and state["calls"] == 1
        assert iq._summary_inflight == {} and iq._summary_cache == {}
        state["fail"] = False
        ok = await one()
        assert ok["assets"]["total"] > 0 and state["calls"] == 2, "the next caller builds again"
    finally:
        await engine.dispose()
