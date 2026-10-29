"""Durable group replay against a private, disposable PostgreSQL fixture."""
import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_ingest_journal as J
from app.services import host_insert_batcher as B
from pg_fixture import pg_cluster, pg_db  # noqa: F401


COLS = ['agent_id', 'timestamp', 'v']
STAMP = datetime(2026, 10, 29, tzinfo=timezone.utc)


def member(token, value=1):
    return token, [[token, STAMP, value]]


class Store:
    def __init__(self):
        self.blocks = {}
        self.calls = []
        self.lose_response = False

    def __call__(self, table, columns, rows, token):
        self.calls.append((table, columns, rows, token))
        previous = self.blocks.setdefault(token, rows)
        assert previous == rows, 'A replay must preserve the entire original block'
        if self.lose_response:
            raise ConnectionError('synthetic response loss')


@asynccontextmanager
async def fixture(pg_db, store, **kwargs):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        yield J.HostIngestJournal(engine, store, **kwargs)
    finally:
        await engine.dispose()


def test_encoding_preserves_exact_values_and_rejects_bad_values():
    rows = [['unicode-\u0627', STAMP, uuid4(), 2**60, 1.125, None, [True, 'nested']]]
    assert J.unpack(J.pack(['c'], rows)) == [['c'], rows]
    with pytest.raises(ValueError):
        J.pack(['c'], [[float('nan')]])
    with pytest.raises(ValueError):
        J.pack(['c'], [[object()]])
    with pytest.raises(J.JournalUnavailable):
        J.pack(['c'], [['x' * J.MAX_PAYLOAD]])
    with pytest.raises(J.JournalUnavailable):
        J.unpack(J.pack(['c'], [[1]]) + b'trailing')


@pytest.mark.asyncio
async def test_crash_after_group_insert_replays_original_group_from_new_process(pg_db):
    store = Store()
    store.lose_response = True
    async with fixture(pg_db, store) as first:
        with pytest.raises(ConnectionError):
            await first.submit('host_cpu_metrics', COLS, [member('a'), member('b')])
    assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'pending'
    assert pg_db.scalar('SELECT count(*) FROM host_ingest_receipts') == 2
    original = store.calls[0]
    store.lose_response = False
    async with fixture(pg_db, store) as replacement:
        # Only one original member reappears, mixed with a new upload. Clock
        # correction may differ: the first durably recorded payload wins.
        await replacement.submit('host_cpu_metrics', COLS, [member('b', 99), member('c')])
        await replacement.submit('host_cpu_metrics', COLS, [member('a')])
    assert store.calls[1] == original
    assert sum(len(rows) for rows in store.blocks.values()) == 3
    assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE state='committed'") == 2
    assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity') == 0
    assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity') == 3


@pytest.mark.asyncio
async def test_crash_before_external_write_is_recovered_without_producer(pg_db):
    store = Store()
    async with fixture(pg_db, store) as first:
        await first.stage('host_cpu_metrics', COLS, [member('a'), member('b')])
    assert not store.calls
    async with fixture(pg_db, store) as replacement:
        await replacement.recover()
    assert len(store.calls) == 1
    assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'committed'


@pytest.mark.asyncio
async def test_receipt_prevents_duplicate_after_clickhouse_dedup_window_expires(pg_db):
    store = Store()
    async with fixture(pg_db, store) as journal:
        await journal.submit('host_cpu_metrics', COLS, [member('a')])
        store.blocks.clear()  # ClickHouse forgot its token; PostgreSQL did not.
        await journal.submit('host_cpu_metrics', COLS, [member('a')])
    assert len(store.calls) == 1


@pytest.mark.asyncio
async def test_old_uncertain_write_is_held_and_blocks_following_batches(pg_db):
    store = Store()
    store.lose_response = True
    async with fixture(pg_db, store) as journal:
        with pytest.raises(ConnectionError):
            await journal.submit('host_cpu_metrics', COLS, [member('a')])
        pg_db.execute("UPDATE host_ingest_batches SET attempted_at=clock_timestamp()-interval '1 hour'")
        store.lose_response = False
        with pytest.raises(J.JournalUnavailable, match='replay window'):
            await journal.submit('host_cpu_metrics', COLS, [member('b')])
        with pytest.raises(J.JournalUnavailable, match='reconciliation'):
            await journal.submit('host_cpu_metrics', COLS, [member('a')])
        # An unrelated table is not blocked by this table's held batch.
        await journal.submit('host_memory_metrics', COLS, [member('c')])
    assert [call[0] for call in store.calls] == ['host_cpu_metrics', 'host_memory_metrics']
    assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE state='held'") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity', ['bytes', 'receipts'])
async def test_capacity_backpressure_retains_known_receipts_and_never_writes_unjournaled(pg_db, capacity):
    store = Store()
    async with fixture(pg_db, store, **({'max_bytes': 1} if capacity == 'bytes' else {'max_receipts': 1})) as journal:
        with pytest.raises(J.JournalUnavailable, match='capacity'):
            await journal.submit('host_cpu_metrics', COLS, [member('a'), member('b')])
    assert not store.calls
    assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches') == 0
    assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity') == 0


@pytest.mark.asyncio
async def test_two_workers_cannot_regroup_while_a_write_is_in_flight(pg_db):
    entered, release = threading.Event(), threading.Event()
    store = Store()
    def writer(*args):
        entered.set()
        assert release.wait(5)
        store(*args)
    async with fixture(pg_db, writer) as first, fixture(pg_db, store) as second:
        writing = asyncio.create_task(first.submit('host_cpu_metrics', COLS, [member('a'), member('b')]))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            with pytest.raises(J.JournalUnavailable, match='busy'):
                await second.submit('host_cpu_metrics', COLS, [member('b'), member('c')])
        finally:
            release.set()
            await writing
        await second.submit('host_cpu_metrics', COLS, [member('b'), member('c')])
    assert sum(len(rows) for rows in store.blocks.values()) == 3


@pytest.mark.asyncio
async def test_cancellation_does_not_release_owner_until_writer_finishes(pg_db):
    entered, release = threading.Event(), threading.Event()
    store = Store()
    def writer(*args):
        entered.set()
        assert release.wait(5)
        store(*args)
    async with fixture(pg_db, writer) as first, fixture(pg_db, store) as second:
        writing = asyncio.create_task(first.submit('host_cpu_metrics', COLS, [member('a')]))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            writing.cancel()
            await asyncio.sleep(0.02)
            with pytest.raises(J.JournalUnavailable, match='busy'):
                await second.drain('host_cpu_metrics')
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writing
        await second.recover()
    assert len(store.blocks) == 1
    assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'committed'


@pytest.mark.asyncio
async def test_batcher_never_uses_raw_presence_after_journal_failure(pg_db, monkeypatch):
    store = Store()
    store.lose_response = True
    async with fixture(pg_db, store) as journal:
        batcher = B.HostInsertBatcher(lambda: None, max_delay_s=0.01, journal=journal)
        monkeypatch.setattr(batcher, '_stored_members', lambda _: pytest.fail('raw presence bypassed journal'))
        with pytest.raises(ConnectionError):
            await batcher.insert('host_cpu_metrics', COLS, member('a')[1], 'a')
    assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'pending'


@pytest.mark.asyncio
async def test_duplicate_member_and_table_identity_are_scoped(pg_db):
    store = Store()
    async with fixture(pg_db, store) as journal:
        await journal.submit('host_cpu_metrics', COLS, [member('a'), member('a')])
        await journal.submit('host_memory_metrics', COLS, [member('a')])
    assert len(store.blocks) == 2
    assert all(len(rows) == 1 for rows in store.blocks.values())
