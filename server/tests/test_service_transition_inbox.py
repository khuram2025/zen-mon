import asyncio
import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.services import service_transition_inbox as inbox, notification_outbox
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_alert_funnel import world  # noqa: F401


def prepare(w):
    check=w.db.scalar("INSERT INTO service_checks(name,check_type,target_host,status) VALUES('Durable HTTP','http','example.invalid','up') RETURNING id")
    w.db.execute("UPDATE alert_rules SET device_id=NULL,service_check_id=$1,metric='service_status',trigger_on='down',recovery_alert=true WHERE id=$2",check,w.rule)
    return check


def enqueue(w,check,old,new):
    return w.db.scalar("INSERT INTO service_transition_inbox(service_check_id,observed_at,payload) VALUES($1,clock_timestamp(),$2::jsonb) RETURNING id",
        check,{'service_check_id':str(check),'check_name':'Durable HTTP','check_type':'http','old_status':old,'new_status':new,'target':'https://example.invalid'})


@asynccontextmanager
async def factory(w):
    engine=create_async_engine(w.db.url,poolclass=NullPool)
    try:yield async_sessionmaker(engine,expire_on_commit=False)
    finally:await engine.dispose()


@pytest.mark.asyncio
async def test_api_absence_preserves_trigger_then_recovery_and_notifications(world):
    w=world;check=prepare(w)
    enqueue(w,check,'up','down');enqueue(w,check,'down','up')
    assert w.db.scalar('SELECT count(*) FROM alerts')==0
    async with factory(w) as sf:
        assert await inbox.consume_one(sf)
        assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'")==1
        assert await inbox.consume_one(sf)
        assert not await inbox.consume_one(sf)
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'")==0
    assert w.db.scalar('SELECT count(*) FROM service_transition_inbox WHERE processed_at IS NOT NULL')==2
    assert w.db.scalar('SELECT count(*) FROM notification_outbox')==2
    assert not w.mails and not w.hooks


@pytest.mark.asyncio
async def test_notification_enqueue_failure_rolls_back_incident_and_consume_marker(world,monkeypatch):
    w=world;check=prepare(w);enqueue(w,check,'up','down');enqueue(w,check,'down','up')
    original=notification_outbox.enqueue
    async def fail(*args):raise RuntimeError('injected notification transaction failure')
    monkeypatch.setattr(notification_outbox,'enqueue',fail)
    async with factory(w) as sf:
        assert await inbox.consume_one(sf)
        assert w.db.scalar('SELECT count(*) FROM alerts')==0
        assert w.db.scalar('SELECT count(*) FROM service_transition_inbox WHERE processed_at IS NOT NULL')==0
        assert not await inbox.consume_one(sf),'Recovery must not overtake a failed trigger'
        monkeypatch.setattr(notification_outbox,'enqueue',original)
        w.db.execute('UPDATE service_transition_inbox SET due_at=clock_timestamp()')
        assert await inbox.consume_one(sf)
        assert await inbox.consume_one(sf)
    assert w.db.scalar('SELECT count(*) FROM notification_outbox')==2


@pytest.mark.asyncio
async def test_two_workers_consume_once(world):
    w=world;check=prepare(w);enqueue(w,check,'up','down')
    async with factory(w) as sf:
        results=await asyncio.gather(inbox.consume_one(sf),inbox.consume_one(sf))
    assert sorted(results)==[False,True]
    assert w.db.scalar('SELECT count(*) FROM notification_outbox')==1


@pytest.mark.parametrize('fail',[False,True])
def test_go_producer_commits_state_and_work_atomically(pg_db,fail):
    go=shutil.which('go') or ('/usr/local/go/bin/go' if Path('/usr/local/go/bin/go').exists() else None)
    if go is None:pytest.skip('Go executable unavailable for cross-language fixture')
    check=pg_db.scalar("INSERT INTO service_checks(name,check_type,target_host,status) VALUES('fixture','http','example.invalid','down') RETURNING id")
    assert pg_db.scalar("SELECT poller_id FROM service_monitoring_vantages WHERE service_check_id=$1",check)=='central'
    pg_db.psql('GRANT SELECT,UPDATE ON service_checks TO zenplus; GRANT SELECT ON service_monitoring_vantages TO zenplus;')
    if fail:
        pg_db.psql("CREATE FUNCTION reject_service_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture rejection'; END $$; CREATE TRIGGER reject_service_event BEFORE INSERT ON service_transition_inbox FOR EACH ROW EXECUTE FUNCTION reject_service_event();")
    env=os.environ.copy();env.update(ZENPLUS_SERVICE_INBOX_TEST_DSN=pg_db.url_for('zenplus').replace('postgresql+asyncpg://','postgres://'),
        ZENPLUS_SERVICE_INBOX_TEST_ID=str(check),ZENPLUS_SERVICE_INBOX_EXPECT_FAILURE='1' if fail else '0',GOTOOLCHAIN='local')
    result=subprocess.run([go,'test','./internal/store','-run','^TestServiceTransitionAtomic$','-count=1'],
        cwd=Path(__file__).resolve().parents[2]/'poller',env=env,capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stdout+result.stderr
    assert pg_db.scalar('SELECT status FROM service_checks WHERE id=$1',check)==('down' if fail else 'up')
    assert pg_db.scalar('SELECT count(*) FROM service_transition_inbox')==(0 if fail else 1)
