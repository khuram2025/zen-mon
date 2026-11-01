"""Execute the real Go collector insert paths against disposable PostgreSQL."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from app.services.host_ingest_journal import HostIngestJournal
from pg_fixture import pg_cluster, pg_db  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_flow_tables_cross_language_recovery_and_failed_commit(pg_db, fail):
    go = shutil.which('go')
    if not go:
        pytest.skip('Go executable required')
    if fail:
        pg_db.psql("CREATE FUNCTION reject_flow_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$; CREATE TRIGGER reject_flow_receipt BEFORE INSERT ON host_ingest_receipts FOR EACH ROW EXECUTE FUNCTION reject_flow_receipt();")
    env = os.environ.copy()
    env.update(ZENPLUS_FLOW_JOURNAL_TEST_DSN=pg_db.url_for('zenplus').replace('postgresql+asyncpg://', 'postgres://'),
               ZENPLUS_FLOW_JOURNAL_EXPECT_FAILURE='1' if fail else '0', GOTOOLCHAIN='local')
    result = subprocess.run([go, 'test', './cmd/netflow-collector', '-run', '^TestFlowJournalPrivatePostgres$', '-count=1'],
                            cwd=Path(__file__).resolve().parents[2]/'poller', env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout+result.stderr
    assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches') == (0 if fail else 5)
    assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity') == (0 if fail else 5)
    if fail:
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity') == 0
        return
    engine = create_async_engine(pg_db.url_for('zenplus'), poolclass=NullPool)
    blocks = {}
    calls = {}
    def write(table, columns, rows, token):
        values = dict(zip(columns, rows[0], strict=True))
        if table == 'flow_records':
            assert values['bytes'] == 2**64-1
            assert values['src_addr6'] == '2001:db8::10'
            assert values['exporter_ip6'] == '2001:db8::62'
            assert values['input_if'] == 70000 and values['input_snmp'] == 0
        elif table == 'flow_templates':
            assert values['field_ids'] == [27, 28, 12]
            assert values['field_lengths'] == [16, 16, 4]
            assert values['enterprise_ids'] == [0, 0, 9]
        elif table == 'flow_app_names':
            assert values['name'] == 'lab \u0627'
        elif table == 'flow_if_counters':
            assert values['in_octets'] == 2**64-1 and values['exporter_ip6'] == '2001:db8::90'
        elif table == 'flow_collector_stats':
            assert values['clock_offset_ms'] == -1500 and values['datagrams'] == 3
        else:
            pytest.fail('unexpected table')
        assert blocks.setdefault((table, token), rows) == rows
        calls[table] = calls.get(table, 0)+1
        if calls[table] == 1:
            raise ConnectionError('ambiguous response')
    try:
        for table in ['flow_records', 'flow_templates', 'flow_app_names', 'flow_if_counters', 'flow_collector_stats']:
            with pytest.raises(ConnectionError):
                await HostIngestJournal(engine, write).drain(table)
            await HostIngestJournal(engine, write).drain(table)
        assert len(blocks) == 5 and all(n == 2 for n in calls.values())
        assert pg_db.scalar("SELECT count(*) FROM host_ingest_batches WHERE state='committed'") == 5
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity') == 0
    finally:
        await engine.dispose()
