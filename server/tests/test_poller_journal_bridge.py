import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from datetime import datetime,timezone
from uuid import UUID
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from app.services.host_ingest_journal import HostIngestJournal
from pg_fixture import pg_cluster,pg_db  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('fail',[False,True])
async def test_go_handoff_is_atomic_idempotent_and_recovered_with_original_types(pg_db,fail):
    go=shutil.which('go')
    if not go:pytest.skip('Go executable required')
    if fail:
        pg_db.psql("CREATE FUNCTION reject_poller_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture failure'; END $$; CREATE TRIGGER reject_poller_receipt BEFORE INSERT ON host_ingest_receipts FOR EACH ROW EXECUTE FUNCTION reject_poller_receipt();")
    env=os.environ.copy();env.update(ZENPLUS_ANALYTICS_JOURNAL_TEST_DSN=pg_db.url_for('zenplus').replace('postgresql+asyncpg://','postgres://'),ZENPLUS_ANALYTICS_JOURNAL_EXPECT_FAILURE='1' if fail else '0',GOTOOLCHAIN='local')
    result=subprocess.run([go,'test','./internal/store','-run','^TestAnalyticsJournalPrivatePostgres$','-count=1'],cwd=Path(__file__).resolve().parents[2]/'poller',env=env,capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stdout+result.stderr
    assert pg_db.scalar('SELECT count(*) FROM host_ingest_batches')==(0 if fail else 1)
    assert pg_db.scalar('SELECT receipts FROM host_ingest_capacity')==(0 if fail else 1)
    if fail:
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity')==0
        return
    engine=create_async_engine(pg_db.url_for('zenplus'),poolclass=NullPool)
    blocks={};calls=[]
    def lost_response(table,columns,rows,token):
        assert table=='ping_metrics'
        assert rows==[[UUID('00000000-0000-0000-0000-000000000123'),datetime(2026,11,1,0,0,0,123000,tzinfo=timezone.utc),2**64-1,'192.0.2.9',None,'quotes \'" and unicode \u0627']]
        assert blocks.setdefault(token,rows)==rows
        calls.append(token)
        if len(calls)==1:raise ConnectionError('lost response after accepting block')
    try:
        first=HostIngestJournal(engine,lost_response)
        with pytest.raises(ConnectionError):await first.drain('ping_metrics')
        assert pg_db.scalar('SELECT state FROM host_ingest_batches')=='pending'
        await HostIngestJournal(engine,lost_response).drain('ping_metrics')
        assert len(blocks)==1 and len(calls)==2 and calls[0]==calls[1]
        assert pg_db.scalar('SELECT state FROM host_ingest_batches')=='committed'
        assert pg_db.scalar('SELECT payload_bytes FROM host_ingest_capacity')==0
    finally:await engine.dispose()
