"""Exact census reuse, invalidation and appliance-wide worker scheduling."""
import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services import netflow_census as nc, netflow_rollup as nr
from test_netflow_rollup import (
    live, databases, _connect, _target, _generate_rows, _insert, _wait_for_mutations,
    BASE, RELEASE_PATH,
)


class Parts:
    def __init__(self):
        self.identity = [('uuid', 'CREATE TABLE flow_records')]
        self.parts = [('p1', 'p1_1_1_0', 3, 0, 'hash1'), ('p2', 'p2_1_1_0', 2, 0, 'hash2')]
        self.rows = {
            'p1': {'flow': [[3600, 3, 6, 100, 1, 3, 3600100, 1]],
                   'received': [[7200, 3, 3, 100, 3600100, 3600200, 1]]},
            'p2': {'flow': [[7200, 2, 4, 80, 0, 2, 7200100, 0]],
                   'received': [[7200, 2, 2, 80, 7200100, 7200200, 0]]},
        }
        self.reads = []
        self.on_read = None

    async def query(self, sql, params=None):
        if 'FROM system.tables' in sql:
            return self.identity
        if 'FROM system.parts' in sql:
            return self.parts
        clock = 'received' if 'received_at' in sql else 'flow'
        rows = []
        for partition in params['partitions']:
            self.reads.append((partition, clock))
            if self.on_read:
                self.on_read(self, partition, clock)
            rows.extend([[partition, *row] for row in self.rows[partition][clock]])
        return rows


def prepare(parts, stored=None):
    cache = nc.RawCensusCache(parts.query, 'fixture', stored)
    asyncio.run(cache.prepare({'flow': 'flow WHERE _partition_id', 'received': 'received_at WHERE _partition_id'}))
    return cache


def test_warm_cache_reuses_exact_counts_and_received_bounds_across_partitions():
    parts = Parts()
    cold = prepare(parts)
    warm = prepare(parts, cold.export())
    assert len(parts.reads) == 4
    assert (warm.hits, warm.scanned) == (2, 0)
    assert warm.census('received', 7200, 10800) == {
        7200: {'counted': 5, 'total': 5, 'bytes': 180, 'ts_min_ms': 3600100,
               'ts_max_ms': 7200200, 'unmasked': 1}}
    assert warm.census('flow', 3600, 7200) == cold.census('flow', 3600, 7200)
    assert warm.census('flow', 7200, 7200) == {}


@pytest.mark.parametrize('change', ['insert', 'merge', 'mutation', 'hash', 'schema', 'replacement'])
def test_changed_data_identity_is_rescanned(change):
    parts = Parts()
    saved = prepare(parts).export()
    if change == 'schema':
        parts.identity = [('uuid', 'CREATE TABLE changed')]
    elif change == 'replacement':
        parts.identity = [('different-uuid', 'CREATE TABLE flow_records')]
    else:
        row = list(parts.parts[0])
        row[{'insert': 2, 'merge': 1, 'mutation': 3, 'hash': 4}[change]] = {
            'insert': 4, 'merge': 'p1_1_2_1', 'mutation': 1, 'hash': 'changed'}[change]
        parts.parts[0] = tuple(row)
    warm = prepare(parts, saved)
    assert warm.scanned == (2 if change in ('schema', 'replacement') else 1)


def test_removed_partitions_are_not_returned_or_persisted():
    parts = Parts()
    saved = prepare(parts).export()
    parts.parts = parts.parts[1:]
    warm = prepare(parts, saved)
    assert warm.census('flow', 0, 10000).keys() == {7200}
    assert set(warm.export()['partitions']) == {'p2'}


def test_invalid_cache_payload_is_rescanned():
    parts = Parts()
    saved = prepare(parts).export()
    saved['partitions']['p1']['rows']['flow'] = [[3600, 'invalid']]
    assert prepare(parts, saved).scanned == 1
    saved['format'] += 1
    assert prepare(parts, saved).scanned == 2


def test_insert_during_a_read_is_rechecked_before_persistence():
    parts = Parts()
    def mutate(parts, partition, clock):
        if partition == 'p1' and clock == 'flow':
            parts.parts[0] = ('p1', 'p1_1_2_1', 3, 1, 'new-hash')
            parts.on_read = None
    parts.on_read = mutate
    cache = prepare(parts)
    assert cache.scanned == 3
    assert prepare(parts, cache.export()).scanned == 0


def test_continuously_changing_partition_is_never_cached():
    parts = Parts()
    def mutate(parts, partition, clock):
        if partition == 'p1':
            generation = parts.parts[0][3] + 1
            parts.parts[0] = ('p1', 'changing', 3, generation, str(generation))
    parts.on_read = mutate
    cache = prepare(parts)
    assert 'p1' not in cache.export()['partitions']
    assert 'p2' in cache.export()['partitions']


def test_cache_budget_drops_only_reusable_state(monkeypatch):
    parts = Parts()
    cache = prepare(parts)
    exact = cache.census('received', 0, 10000)
    monkeypatch.setattr(nc, 'MAX_CACHE_BYTES', 100)
    assert cache.export()['partitions'] == {}
    assert cache.census('received', 0, 10000) == exact


def test_cold_reads_use_bounded_partition_batches():
    parts = Parts()
    parts.parts = [(f'p{i:02}', f'part{i}', 3, 0, f'hash{i}') for i in range(40)]
    parts.rows = {row[0]: {'flow': [[3600,3,6,100,1,3,3600100,1]],
                         'received': [[7200,3,3,100,3600100,3600200,1]]} for row in parts.parts}
    sizes = []
    original = parts.query
    async def query(sql, params=None):
        if params and 'partitions' in params:
            sizes.append(len(params['partitions']))
        return await original(sql, params)
    parts.query = query
    cache = prepare(parts)
    assert sizes == [16,16,16,16,8,8]
    assert cache.scanned == 40
    assert cache.census('flow',0,10000)[3600]['total'] == 120


def test_uint64_sum_matches_database_overflow_semantics():
    parts = Parts()
    parts.rows['p1']['received'][0][3] = 2**64 - 1
    parts.rows['p2']['received'][0][3] = 2
    cache = prepare(parts)
    assert cache.census('received', 0, 10000)[7200]['bytes'] == 1


@live
def test_live_cache_matches_uncached_census_and_refreshes_late_data_and_mutations(databases):
    db = databases(*RELEASE_PATH)
    client = _connect()
    t = _target(db)
    rows = _generate_rows(BASE, hours=50, count=800)
    # A received hour can contain records stored in different timestamp days.
    rows = [tuple([row[0], BASE + timedelta(hours=49), *row[2:]]) for row in rows]
    _insert(client, db, rows, batches=1)
    client.command(f'SYSTEM STOP MERGES {db}.flow_records')
    async def run():
        cache = nr._raw_cache(t)
        sql = {clock: nr._partition_census_sql(db, clock) for clock in ('flow', 'received')}
        await cache.prepare(sql)
        async def compare(cache):
            for clock in ('flow', 'received'):
                baseline = await nr.raw_census(t, clock, 0, 2**32-1)
                cached = cache.census(clock, 0, 2**32-1)
                names = nr._FLOW_FIELDS if clock == 'flow' else nr._RECEIVED_FIELDS
                assert {h:{k:r[k] for k in names} for h,r in cached.items()} == baseline
        await compare(cache)
        warm = nr._raw_cache(t, cache.export())
        await warm.prepare(sql)
        assert warm.hits >= 2 and warm.scanned == 0
        extra = _generate_rows(BASE, hours=2, count=20, seed=23)
        _insert(client, db, extra, batches=1)
        late = nr._raw_cache(t, warm.export())
        await late.prepare(sql)
        assert late.scanned == 1
        await compare(late)
        client.command(f'SYSTEM START MERGES {db}.flow_records')
        client.command(f'ALTER TABLE {db}.flow_records UPDATE bytes=bytes+1 WHERE flow_version=9', settings={'mutations_sync':2})
        _wait_for_mutations(client, db)
        client.command(f'SYSTEM STOP MERGES {db}.flow_records')
        mutated = nr._raw_cache(t, late.export())
        await mutated.prepare(sql)
        assert mutated.scanned > 0
        await compare(mutated)
        # Physical deletion must remove cached counts as well.
        partition = next(iter(mutated.entries))
        client.command(f'ALTER TABLE {db}.flow_records DROP PARTITION ID \'{partition}\'')
        removed = nr._raw_cache(t, mutated.export())
        await removed.prepare(sql)
        assert partition not in removed.export()['partitions']
        await compare(removed)
    asyncio.run(run())


PG_URL = os.environ.get('NETFLOW_TEST_PG_URL')


@pytest.mark.skipif(not PG_URL, reason='set NETFLOW_TEST_PG_URL to a disposable fixture')
@pytest.mark.parametrize('outcome', ['success', 'exception', 'cancel'])
def test_live_worker_coordination_and_lock_release(monkeypatch, outcome):
    async def run():
        engine = create_async_engine(PG_URL, pool_size=2, max_overflow=0)
        monkeypatch.setattr(nr, 'engine', engine)
        t = nr.ChTarget(database='fixture_' + uuid.uuid4().hex, client=lambda: None)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def sweep(target, cache):
            calls.append(target.database)
            started.set()
            await release.wait()
            if outcome == 'exception':
                raise RuntimeError('fixture failure')
            return float(nr.SWEEP_INTERVAL_S)
        monkeypatch.setattr(nr, 'sweep_once', sweep)
        try:
            async with engine.begin() as conn:
                await conn.execute(text('CREATE TABLE IF NOT EXISTS system_settings (key text PRIMARY KEY, value jsonb, updated_at timestamptz DEFAULT NOW())'))
            winner = asyncio.create_task(nr.coordinated_sweep(t))
            await asyncio.wait_for(started.wait(), 5)
            assert await nr.coordinated_sweep(t) == nr.MUTATION_RECHECK_S
            if outcome == 'cancel':
                winner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await winner
            else:
                release.set()
                if outcome == 'exception':
                    with pytest.raises(RuntimeError):
                        await winner
                else:
                    assert await winner == nr.SWEEP_INTERVAL_S
                    assert await nr.coordinated_sweep(t) > nr.MUTATION_RECHECK_S
                    assert len(calls) == 1
            # The same pool must remain usable without a leaked session lock.
            async with nr._SweepLease() as lease:
                assert lease.acquired
                row = (await lease.connection.execute(text('SELECT value FROM system_settings WHERE key=:k'),
                    {'k':nr.CENSUS_SETTINGS_KEY+':'+t.database})).scalar()
                assert bool(row) == (outcome == 'success')
        finally:
            await engine.dispose()
    asyncio.run(run())
