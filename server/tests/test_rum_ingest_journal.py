import asyncio
import hashlib
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from fastapi import HTTPException

from app.api.v1 import rum as R
from app.services import rum_ingest_journal as J
from app.services import host_insert_batcher as B
from app.services.host_ingest_journal import HostIngestJournal, JournalUnavailable
from app.services.rum_journal_contract_v1 import COLUMNS_V1, ROLLUP_SQL_V1
from test_rum_production import _event, _request
from pg_fixture import pg_cluster, pg_db  # noqa: F401


@pytest.fixture(autouse=True)
def commissioned(monkeypatch):
    monkeypatch.setenv('RUM_INSERT_JOURNAL_ENABLED', '1')
    monkeypatch.setenv('HOST_INSERT_JOURNAL_ENABLED', '1')
    monkeypatch.setenv('ZENPLUS_CONTROLLER_GRANT', '/fixture/grant')
    monkeypatch.setattr(J, 'permitted', lambda: True)
    from app.core import controller_admission
    monkeypatch.setattr(controller_admission, 'permitted', lambda: True)
    monkeypatch.setattr(B, 'RETRY_WINDOW_S', 0)
    monkeypatch.setattr(R, '_HEALTH', Counter(R._HEALTH))
    monkeypatch.setattr(R, '_HEALTH_META', {**R._HEALTH_META, 'sdk_versions': Counter()})


def row(identity):
    value = [0]*len(COLUMNS_V1)
    value[0] = datetime(2026, 11, 1, 9, 1, 2, 123000, tzinfo=timezone.utc)
    value[-1] = hashlib.sha256(identity.encode()).hexdigest()
    return value


class Analytics:
    def __init__(self):
        self.raw = {}; self.derived = {}; self.calls = []; self.lose_rollup = False
        self.lose_raw = False; self.reject_rollup = False
    def query(self, sql):
        assert sql == 'DESCRIBE TABLE apm_rum_events', 'Raw-row presence cannot prove derived completion'
        return SimpleNamespace(result_rows=[(c, 'String') for c in COLUMNS_V1])
    def insert(self, table, rows, **kwargs):
        token = kwargs['settings']['insert_deduplication_token']
        assert table == 'apm_rum_events'
        assert self.raw.setdefault(token, rows) == rows
        self.calls.append(('raw', token))
        if self.lose_raw: raise ConnectionError('lost response after raw acceptance')
    def command(self, sql, parameters, settings):
        assert sql == ROLLUP_SQL_V1
        if self.reject_rollup: raise ConnectionError('derived step unavailable before acceptance')
        assert parameters['batch_from_ms'] % 1000 == 123
        assert parameters['batch_to_ms'] % 1000 == 123
        token = settings['insert_deduplication_token']
        assert self.derived.setdefault(token, parameters) == parameters
        self.calls.append(('rollup', token))
        if self.lose_rollup: raise ConnectionError('lost response after derived acceptance')


def test_version_one_operation_is_frozen_and_configuration_is_guarded(monkeypatch):
    assert COLUMNS_V1 == R.RUM_COLUMNS
    assert ROLLUP_SQL_V1 == R._RUM_ROLLUP_INSERT_SQL
    monkeypatch.delenv('HOST_INSERT_JOURNAL_ENABLED')
    with pytest.raises(RuntimeError): J.validate_configuration()


@pytest.mark.asyncio
async def test_raw_and_derived_replay_keep_original_group_after_regrouping(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    ch = Analytics(); ch.lose_rollup = True
    writer = lambda table, columns, rows, token: J.write(lambda: ch, columns, rows, token)
    try:
        fresh = await J.admit(engine, ['a', 'b'], [row('a'), row('b')])
        assert len(fresh) == 2
        with pytest.raises(ConnectionError):
            await HostIngestJournal(engine, writer).drain(J.WORK_KIND)
        assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'pending'
        assert len(ch.raw) == len(ch.derived) == 1
        fresh = await J.admit(engine, ['b', 'c'], [row('b'), row('c')])
        assert fresh == {hashlib.sha256(b'c').digest()}
        ch.lose_rollup = False
        await HostIngestJournal(engine, writer).recover()
        assert len(ch.raw) == len(ch.derived) == 2
        assert sum(len(rows) for rows in ch.raw.values()) == 3
        assert sum(len(p['dedupe_ids']) for p in ch.derived.values()) == 3
        assert ch.calls[0:2] == ch.calls[2:4]
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE state<>'committed'") == 0
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity') == 0
        assert not await J.admit(engine, ['a', 'b', 'c'], [row('a'), row('b'), row('c')])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_request_member_and_failed_admission_do_not_fake_acceptance(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        assert len(await J.admit(engine, ['a', 'a'], [row('a'), row('a')])) == 1
        assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity') == 1
        with pytest.raises(ValueError): await J.admit(engine, ['other'], [row('a')])
        assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity') == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['raw_response_loss', 'derived_rejection'])
async def test_recovery_completes_missing_derived_step_after_raw_acceptance(pg_db, failure):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    ch = Analytics(); ch.lose_raw = failure == 'raw_response_loss'
    ch.reject_rollup = failure == 'derived_rejection'
    writer = lambda table, columns, rows, token: J.write(lambda: ch, columns, rows, token)
    try:
        await J.admit(engine, ['a', 'b'], [row('a'), row('b')])
        with pytest.raises(ConnectionError):
            await HostIngestJournal(engine, writer).drain(J.WORK_KIND)
        assert len(ch.raw) == 1 and not ch.derived
        assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'pending'
        ch.lose_raw = ch.reject_rollup = False
        await HostIngestJournal(engine, writer).recover()
        assert len(ch.raw) == len(ch.derived) == 1
        assert pg_db.scalar('SELECT state FROM host_ingest_batches') == 'committed'
        assert ch.calls[0] == ch.calls[1]  # The original raw block, never regrouped.
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_bypasses_volatile_dedupe_and_waits_for_commit(monkeypatch):
    events = [_event(event_id='event-00000001'), _event(event_id='event-00000002')]
    async def parse(request): return events
    async def auth(*args, **kwargs):
        return {'id': 'key', 'application_id': 'portal', 'origin_allowlist': ['https://portal.example.com'], 'env_name': 'fixture'}
    async def quota(*args): pass
    async def no_dedupe(*args): raise AssertionError('volatile dedupe must not acknowledge journaled RUM')
    entered = asyncio.Event(); release = asyncio.Event()
    async def admit(engine, identities, rows):
        entered.set(); await release.wait()
        assert rows[0][-1] == hashlib.sha256(identities[0].encode()).hexdigest()
        return {hashlib.sha256(identities[0].encode()).digest()}
    monkeypatch.setattr(R, '_parse_payload', parse)
    monkeypatch.setattr(R, 'authenticate_ingest_key', auth)
    monkeypatch.setattr(R, '_enforce_quota', quota)
    monkeypatch.setattr(R, '_dedupe', no_dedupe)
    monkeypatch.setattr(J, 'admit', admit)
    call = asyncio.create_task(R._ingest_rum(_request(), object()))
    await asyncio.wait_for(entered.wait(), 1); assert not call.done()
    release.set(); response = await call
    assert response.status_code == 202
    assert response.headers['X-RUM-Accepted'] == response.headers['X-RUM-Duplicate'] == '1'
    assert response.headers['X-RUM-Admission'] == 'durable-journal'
    async def failed(*args): raise ConnectionError('private database failure')
    monkeypatch.setattr(J, 'admit', failed)
    with pytest.raises(HTTPException) as error: await R._ingest_rum(_request(), object())
    assert error.value.status_code == 503 and 'private' not in error.value.detail
