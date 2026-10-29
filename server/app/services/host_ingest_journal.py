"""Opt-in durable group identity for host telemetry on a commissioned HA cell.

PostgreSQL commits the exact block before ClickHouse sees it. Receipt identities
outlive the block payload; a retry in a different process joins the original
batch, including its other members. Unknown outcomes never use raw presence.

This is deliberately bounded and fail-closed: receipts are not automatically
pruned, old uncertain writes are held, and capacity exhaustion returns an error
to the producer spool. Commissioning must qualify the ClickHouse deduplication
window and forbid other writers to these tables. It is not an unlimited
exactly-once contract, nor a journal for the Go/APM/flow producers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import zlib
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text

logger = logging.getLogger(__name__)
MAX_PAYLOAD = 8 * 1024 * 1024
MAX_ROWS = 20000
MAX_MEMBERS = 2000
MAX_BYTES = 512 * 1024 * 1024
MAX_RECEIPTS = 10_000_000
REPLAY_SECONDS = 300
LOCK_NAMESPACE = 17524001


class JournalUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get('HOST_INSERT_JOURNAL_ENABLED') == '1'


def _encode(value):
    if isinstance(value, datetime):
        return ['datetime', value.isoformat()]
    if isinstance(value, UUID):
        return ['uuid', str(value)]
    if isinstance(value, (tuple, list)):
        return ['list', [_encode(v) for v in value]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return ['scalar', value]
    raise ValueError('unsupported telemetry value type')


def _decode(value):
    kind, data = value
    if kind == 'datetime':
        return datetime.fromisoformat(data)
    if kind == 'uuid':
        return UUID(data)
    if kind == 'list':
        return [_decode(v) for v in data]
    if kind == 'scalar':
        return data
    raise ValueError('unsupported journal encoding')


def pack(columns, rows):
    if len(rows) > MAX_ROWS:
        raise JournalUnavailable('host journal batch row limit exceeded')
    raw = json.dumps(_encode([columns, rows]), ensure_ascii=False, allow_nan=False,
                     separators=(',', ':')).encode()
    if len(raw) > MAX_PAYLOAD:
        raise JournalUnavailable('host journal batch byte limit exceeded')
    return zlib.compress(raw)


def unpack(payload):
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload, MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD or not decoder.eof or decoder.unused_data:
        raise JournalUnavailable('invalid or oversized journal payload')
    return _decode(json.loads(raw))


def _key(table):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', table):
        raise ValueError('journal requires an unqualified telemetry table name')
    return int.from_bytes(hashlib.sha256(table.encode()).digest()[:4], 'big', signed=True)


def _token(token):
    if not token or len(token) > 256:
        raise ValueError('invalid host upload identity')
    return hashlib.sha256(token.encode()).digest()


class HostIngestJournal:
    def __init__(self, engine, writer, *, max_bytes=MAX_BYTES,
                 max_receipts=MAX_RECEIPTS, replay_seconds=REPLAY_SECONDS):
        self.engine = engine
        self.writer = writer
        self.max_bytes = max_bytes
        self.max_receipts = max_receipts
        self.replay_seconds = replay_seconds

    @asynccontextmanager
    async def _locked(self, table):
        """Pin a PG session across the pre-write commit and ClickHouse attempt.

        A pooled connection must never retain this session lock. Invalidating
        on cleanup failure closes its backend rather than returning it to pool.
        """
        params = {'namespace': LOCK_NAMESPACE, 'key': _key(table)}
        async with self.engine.connect() as conn:
            locked = False
            try:
                locked = (await conn.execute(text(
                    'SELECT pg_try_advisory_lock(:namespace,:key)'), params)).scalar()
                await conn.commit()
                if not locked:
                    raise JournalUnavailable('host journal table is busy; retry upload')
                yield conn
            finally:
                if locked:
                    try:
                        await conn.rollback()
                        await conn.execute(text('SELECT pg_advisory_unlock(:namespace,:key)'), params)
                        await conn.commit()
                    except BaseException:
                        await conn.invalidate()
                        raise

    async def stage(self, table, columns, members):
        """First use of an upload identity freezes its rows and grouping.

        Identity comes from the authenticated agent + batch id + metric kind.
        Replays reuse the first payload even if clock correction is recomputed.
        A client must never reuse that identity for a different logical upload.
        """
        if not members or len(members) > MAX_MEMBERS:
            raise JournalUnavailable('host journal member limit exceeded')
        unique = {}
        for token, rows in members:
            unique.setdefault(_token(token), rows)
        async with self._locked(table) as conn:
            existing = (await conn.execute(text('''
                SELECT token_hash,batch_id FROM host_ingest_receipts
                WHERE table_name=:table AND token_hash=ANY(:tokens)
            '''), {'table': table, 'tokens': list(unique)})).all()
            identities = {bytes(row[0]): row[1] for row in existing}
            fresh = sorted(set(unique) - identities.keys())
            if fresh:
                payload = pack(columns, [r for token in fresh for r in unique[token]])
                capacity = (await conn.execute(text('''
                    SELECT payload_bytes,receipts FROM host_ingest_capacity
                    WHERE singleton FOR UPDATE
                '''))).one()
                if capacity[0] + len(payload) > self.max_bytes or capacity[1] + len(fresh) > self.max_receipts:
                    raise JournalUnavailable('host journal capacity exhausted; producer must retain upload')
                batch_id = (await conn.execute(text('''
                    INSERT INTO host_ingest_batches(table_name,write_token,payload)
                    VALUES(:table,:token,:payload) RETURNING id
                '''), {'table': table, 'token': 'host-journal-v1-' + str(uuid4()), 'payload': payload})).scalar_one()
                await conn.execute(text('''
                    INSERT INTO host_ingest_receipts(table_name,token_hash,batch_id)
                    VALUES(:table,:token,:batch)
                '''), [{'table': table, 'token': token, 'batch': batch_id} for token in fresh])
                await conn.execute(text('''
                    UPDATE host_ingest_capacity SET payload_bytes=payload_bytes+:bytes,
                    receipts=receipts+:count WHERE singleton
                '''), {'bytes': len(payload), 'count': len(fresh)})
                identities.update({token: batch_id for token in fresh})
            await conn.commit()  # Must precede every possible external write.
            return sorted(set(identities.values()))

    async def drain(self, table, *, limit=8):
        async with self._locked(table) as conn:
            for _ in range(limit):
                row = (await conn.execute(text('''
                    SELECT id,write_token,payload,state,attempted_at,
                           clock_timestamp() AS db_now
                    FROM host_ingest_batches WHERE table_name=:table AND state<>'committed'
                    ORDER BY id LIMIT 1
                '''), {'table': table})).mappings().first()
                if row is None:
                    return
                if row['state'] == 'held':
                    raise JournalUnavailable('host journal requires uncertain-write reconciliation')
                if row['attempted_at'] is not None and (
                        row['db_now'] - row['attempted_at']).total_seconds() >= self.replay_seconds:
                    await conn.execute(text('''
                        UPDATE host_ingest_batches SET state='held',hold_reason='replay_window_expired'
                        WHERE id=:id
                    '''), {'id': row['id']})
                    await conn.commit()
                    raise JournalUnavailable('uncertain host insert exceeded automatic replay window')
                columns, rows = unpack(row['payload'])
                await conn.execute(text('''
                    UPDATE host_ingest_batches SET attempted_at=COALESCE(attempted_at,clock_timestamp())
                    WHERE id=:id
                '''), {'id': row['id']})
                await conn.commit()
                # Do not release the PG owner while a cancelled to_thread is
                # still performing its write. Process death relies on fencing.
                writing = asyncio.create_task(asyncio.to_thread(
                    self.writer, table, columns, rows, row['write_token']))
                try:
                    await asyncio.shield(writing)
                except asyncio.CancelledError:
                    try:
                        await writing
                    finally:
                        raise
                await conn.execute(text('''
                    UPDATE host_ingest_batches SET state='committed',payload=NULL,
                    committed_at=clock_timestamp() WHERE id=:id
                '''), {'id': row['id']})
                await conn.execute(text('''
                    UPDATE host_ingest_capacity SET payload_bytes=payload_bytes-:bytes WHERE singleton
                '''), {'bytes': len(row['payload'])})
                await conn.commit()

    async def submit(self, table, columns, members):
        ids = await self.stage(table, columns, members)
        if await self._committed(ids):
            return
        await self.drain(table)
        if not await self._committed(ids):
            raise JournalUnavailable('host journal has pending uploads; retry')

    async def _committed(self, ids):
        async with self.engine.connect() as conn:
            incomplete = (await conn.execute(text('''
                SELECT count(*) FROM host_ingest_batches WHERE id=ANY(:ids) AND state<>'committed'
            '''), {'ids': ids})).scalar_one()
        return not incomplete

    async def recover(self):
        async with self.engine.connect() as conn:
            tables = (await conn.execute(text('''
                SELECT DISTINCT table_name FROM host_ingest_batches WHERE state='pending'
            '''))).scalars().all()
        for table in tables:
            try:
                await self.drain(table)
            except Exception as exc:
                # No payload, token, connection string or provider exception.
                logger.warning('host journal recovery for %s deferred (%s)', table, type(exc).__name__)


async def recovery_loop():
    from app.core.database import engine, get_clickhouse_client
    from app.services.host_insert_batcher import HostInsertBatcher
    journal = HostIngestJournal(engine, HostInsertBatcher(get_clickhouse_client)._write)
    while True:
        try:
            from app.core.controller_admission import permitted
            if permitted():
                await journal.recover()
        except Exception as exc:
            logger.warning('host journal recovery unavailable (%s)', type(exc).__name__)
        await asyncio.sleep(2)
