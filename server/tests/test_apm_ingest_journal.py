import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import apm_ingest as A
from app.services import apm_ingest_journal as J
from app.services.host_ingest_journal import HostIngestJournal, JournalUnavailable, JournalLimitExceeded, pack, unpack
from pg_fixture import pg_cluster, pg_db  # noqa: F401


@pytest.fixture(autouse=True)
def commissioned(monkeypatch):
    monkeypatch.setattr(A, 'STATS', dict(A.STATS))
    monkeypatch.setattr(A, '_PENDING_STATS', dict(A._PENDING_STATS))
    monkeypatch.setattr(A, '_last_received_at', None)
    monkeypatch.setenv('APM_INSERT_JOURNAL_ENABLED', '1')
    monkeypatch.setenv('HOST_INSERT_JOURNAL_ENABLED', '1')
    monkeypatch.setenv('ZENPLUS_CONTROLLER_GRANT', '/fixture/grant')
    monkeypatch.setattr(J, 'permitted', lambda: True)
    from app.core import controller_admission
    monkeypatch.setattr(controller_admission, 'permitted', lambda: True)


def payload(*ids, error=True):
    now = time.time_ns()
    return {'resourceSpans': [{'resource': {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'journal-lab'}},
    ]}, 'scopeSpans': [{'spans': [
        {'traceId': f'{n:032x}', 'spanId': f'{n:016x}', 'name': 'test',
         'startTimeUnixNano': str(now), 'endTimeUnixNano': str(now+1234567),
         'kind': 'SPAN_KIND_SERVER', 'status': {'code': 'STATUS_CODE_ERROR' if error else 'STATUS_CODE_OK'},
         'attributes': [
             {'key': 'label', 'value': {'stringValue': 'quote \' and \u0627'}},
             {'key': 'number', 'value': {'doubleValue': 1.125}},
             {'key': 'flag', 'value': {'boolValue': True}},
         ]} for n in ids]}]}]}


def decoded(body):
    return A.decode_otlp_traces_json(body, 'fixture')[:3]


def test_map_codec_and_configuration(monkeypatch):
    rows = [[{'text': '\u0627', 'number': 2**64-1, 'flag': True, 'nested': [None, {'x': 1.125}]}]]
    assert unpack(pack(['map'], rows)) == [['map'], rows]
    with pytest.raises(ValueError): pack(['map'], [[{1: 'not a string key'}]])
    monkeypatch.delenv('HOST_INSERT_JOURNAL_ENABLED')
    with pytest.raises(RuntimeError): J.validate_configuration()
    monkeypatch.setenv('HOST_INSERT_JOURNAL_ENABLED', '1')
    monkeypatch.delenv('ZENPLUS_CONTROLLER_GRANT')
    with pytest.raises(RuntimeError): J.validate_configuration()


@pytest.mark.asyncio
async def test_related_tables_roll_back_when_last_receipt_fails(pg_db):
    pg_db.psql("""CREATE FUNCTION reject_resource_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.table_name='apm_traces_resource' THEN RAISE EXCEPTION 'fixture failure'; END IF;
        RETURN NEW; END $$; CREATE TRIGGER reject_resource_receipt BEFORE INSERT ON host_ingest_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_resource_receipt();""")
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        body = payload(1)
        with pytest.raises(Exception, match='fixture failure'):
            await J.admit(engine, uuid4(), json.dumps(body).encode(), *decoded(body))
        assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches') == 0
        assert pg_db.scalar('SELECT count(*) FROM host_ingest_receipts') == 0
        assert pg_db.scalar('SELECT payload_bytes+receipts FROM host_ingest_capacity') == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_busy_related_table_rolls_back_and_releases_earlier_locks(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        journal = HostIngestJournal(engine, None)
        async with journal._locked('apm_traces_resource'):
            body = payload(1)
            with pytest.raises(JournalUnavailable):
                await J.admit(engine, 'key', json.dumps(body).encode(), *decoded(body))
        assert pg_db.scalar('SELECT count(*) FROM host_ingest_receipts') == 0
        await J.admit(engine, 'key', json.dumps(body).encode(), *decoded(body))
        assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches') == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_related_capacity_failure_does_not_leave_partial_admission(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        journal = HostIngestJournal(engine, None, max_receipts=2)
        with pytest.raises(JournalUnavailable, match='capacity exhausted'):
            await journal.stage_many([(table, ['v'], [('member', [[1]])])
                for table in ['apm_spans', 'apm_exceptions', 'apm_traces_resource']])
        assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches') == 0
        assert pg_db.scalar('SELECT payload_bytes+receipts FROM host_ingest_capacity') == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_regrouped_retry_recovers_original_spans_maps_and_exception_ids(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    calls = []; stored = {}; lose = [True]
    def writer(table, columns, rows, token):
        assert stored.setdefault((table, token), rows) == rows
        calls.append((table, token, rows))
        if table == 'apm_spans' and lose[0]:
            raise ConnectionError('lost response after accepting span block')
    try:
        key = uuid4(); body = payload(1, 2)
        initial = await J.admit(engine, key, json.dumps(body).encode(), *decoded(body))
        with pytest.raises(ConnectionError):
            await HostIngestJournal(engine, writer).drain('apm_spans')
        # Different grouping and regenerated exception UUIDs must not replace
        # either original span or its matching exception.
        regrouped = payload(2, 3)
        await J.admit(engine, key, json.dumps(regrouped).encode(), *decoded(regrouped))
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_receipts WHERE table_name='apm_spans'") == 3
        lose[0] = False
        await HostIngestJournal(engine, writer).recover()
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE state<>'committed'") == 0
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity') == 0
        spans = [r for (t, _), rows in stored.items() if t == 'apm_spans' for r in rows]
        errors = [r for (t, _), rows in stored.items() if t == 'apm_exceptions' for r in rows]
        assert len(spans) == len(errors) == 3
        first_rows = next(rows for t, _, rows in calls if t == 'apm_spans')
        assert sum(t == 'apm_spans' and rows == first_rows for t, _, rows in calls) == 2
        assert {r[A.SPAN_COLUMNS.index('trace_id')] for r in spans} == {f'{n:032x}' for n in (1, 2, 3)}
        row = spans[0]
        assert row[A.SPAN_COLUMNS.index('attributes_number')]['number'] == 1.125
        assert row[A.SPAN_COLUMNS.index('attributes_bool')]['flag'] == 1
        assert row[A.SPAN_COLUMNS.index('attributes_string')]['label'].endswith('\u0627')
        assert len({r[1] for r in errors}) == 3
        assert initial['apm_spans']
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_exception_receipt_freezes_first_admitted_span(pg_db):
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    try:
        body = payload(1, error=False)
        await J.admit(engine, 'key', json.dumps(body).encode(), *decoded(body))
        assert pg_db.scalar("SELECT state FROM host_ingest_batches WHERE table_name='apm_exceptions'") == 'committed'
        changed = payload(1, error=True)
        await J.admit(engine, 'key', json.dumps(changed).encode(), *decoded(changed))
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_receipts WHERE table_name='apm_exceptions'") == 1
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE table_name='apm_exceptions' AND payload IS NOT NULL") == 0
        # Another authenticated key is a different admission namespace.
        await J.admit(engine, 'other-key', json.dumps(changed).encode(), *decoded(changed))
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_receipts WHERE table_name='apm_spans'") == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_waits_for_durable_admission_and_returns_retryable_failure(monkeypatch):
    body = json.dumps(payload(1)).encode(); entered = asyncio.Event(); release = asyncio.Event()
    async def read(): return body
    async def auth(*args, **kwargs): return {'id': 'key', 'env_name': 'fixture'}
    async def admit(*args): entered.set(); await release.wait()
    monkeypatch.setattr(A, 'authenticate_ingest_key', auth)
    monkeypatch.setattr(J, 'admit', admit)
    monkeypatch.setattr(A, '_queue', None)
    call = asyncio.create_task(A.otlp_traces(SimpleNamespace(body=read), Response(),
        authorization='', content_type='application/json', content_encoding='identity', db=object()))
    await asyncio.wait_for(entered.wait(), 1)
    assert not call.done()
    release.set(); await call
    async def unavailable(*args): raise ConnectionError('private database failure detail')
    monkeypatch.setattr(J, 'admit', unavailable)
    with pytest.raises(HTTPException) as error:
        await A.otlp_traces(SimpleNamespace(body=read), Response(), authorization='',
            content_type='application/json', content_encoding='identity', db=object())
    assert error.value.status_code == 503
    assert 'private' not in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,status', [(ValueError('private input'), 400),
    (JournalLimitExceeded('private rows'), 413)])
async def test_http_rejects_invalid_or_oversized_unadmitted_payload(monkeypatch, failure, status):
    body = json.dumps(payload(1)).encode()
    async def read(): return body
    async def auth(*args, **kwargs): return {'id': 'key', 'env_name': 'fixture'}
    async def fail(*args): raise failure
    monkeypatch.setattr(A, 'authenticate_ingest_key', auth)
    monkeypatch.setattr(J, 'admit', fail)
    with pytest.raises(HTTPException) as error:
        await A.otlp_traces(SimpleNamespace(body=read), Response(), authorization='',
            content_type='application/json', content_encoding='identity', db=object())
    assert error.value.status_code == status
    assert 'private' not in error.value.detail
