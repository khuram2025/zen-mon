"""Real PostgreSQL NCM contracts, gated to a disposable private-socket cluster."""
import os
import pytest
if os.environ.get('ZENPLUS_NCM_INTEGRATION')!='1':
    pytest.skip('Requires isolated NCM PostgreSQL fixture', allow_module_level=True)
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
url=urlsplit(os.environ['DATABASE_URL'])
socket=Path(parse_qs(url.query)['host'][0]).resolve()
assert url.username=='ncm_fixture' and url.port==15433
assert socket.parent.parent==Path('/tmp') and socket.parent.name.startswith('zenplus-ncm-integration-')

from types import SimpleNamespace as NS
from uuid import uuid4,UUID
import hashlib
from unittest.mock import AsyncMock
import pytest_asyncio
from fastapi import FastAPI,HTTPException
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from sqlalchemy.pool import NullPool
from app.api.v1 import ncm
from app.core import security
from app.core.database import get_db
from app.services import ncm_events,ncm_maintenance
from app.services.ncm_content import read_content


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine=create_async_engine(os.environ['DATABASE_URL'],poolclass=NullPool)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    async with sessions() as db:
        await db.execute(text('TRUNCATE devices,ncm_credentials,notification_channels,ncm_audit CASCADE'))
        await db.commit()
        actor=NS(id=uuid4(),username='fixture',role='admin',scope_tags=[])
        async def permissions(db,role):
            return {'admin':['system.admin'],'operator':['ncm.view','ncm.manage'],
                    'viewer':['ncm.view'],'devices':['devices.manage']}[role]
        monkeypatch.setattr(security,'get_role_permissions',permissions)
        import app.core.scoping as scoping
        monkeypatch.setattr(scoping,'get_role_permissions',permissions)
        yield db,sessions,actor
    await engine.dispose()


async def device(db,tag='a',**settings):
    did=uuid4()
    await db.execute(text("INSERT INTO devices(id,hostname,ip_address,device_type,tags) VALUES(:id,'fixture','192.0.2.1','router',CAST(:tags AS jsonb))"),{'id':did,'tags':'["'+tag+'"]'})
    cred=(await db.execute(text("INSERT INTO ncm_credentials(name,username) VALUES('fixture','fixture') RETURNING id"))).scalar()
    await db.execute(text("INSERT INTO device_ncm(device_id,credential_id,platform,keep_versions) VALUES(:id,:cred,'cisco_ios',2)"),{'id':did,'cred':cred})
    await db.commit()
    return did


async def capture(db,did,n,**kw):
    result=await ncm._save_config_version(db,did,'running',f'hostname edge{n}\nenable secret 9 fixture{n}\nend\n','ssh',None,**kw)
    await db.commit()
    return UUID(result['version_id'])


@pytest.mark.asyncio
async def test_retention_dedup_and_last_validated(env):
    db,_,actor=env
    did=await device(db)
    first=await capture(db,did,0,validated=True,actor=actor)
    assert await capture(db,did,0,validated=True,actor=actor)==first
    pin=await capture(db,did,1,actor=actor)
    await db.execute(text('UPDATE device_configs SET pinned=true WHERE id=:id'),{'id':pin})
    await db.commit()
    for n in range(2,9): await capture(db,did,n,actor=actor)
    rows=(await db.execute(text('SELECT id,content,content_enc,content_hash FROM device_configs WHERE device_id=:id'),{'id':did})).fetchall()
    assert len(rows)==4 and first in {r.id for r in rows} and pin in {r.id for r in rows}
    assert all(r.content=='' and 'hostname edge' in read_content(r) for r in rows)


@pytest.mark.asyncio
async def test_transactional_event_and_outbox_retry(env):
    db,_,actor=env
    did=await device(db)
    channel=(await db.execute(text("INSERT INTO notification_channels(type,config,enabled) VALUES('webhook','{}',true) RETURNING id"))).scalar()
    await db.execute(text('UPDATE device_ncm SET notify_channels=:ids WHERE device_id=:id'),{'ids':[channel],'id':did})
    await db.commit()
    await capture(db,did,1,actor=actor)
    await capture(db,did,2,actor=actor)
    row=(await db.execute(text('SELECT * FROM ncm_notification_outbox'))).first()
    assert row and 'fixture2' not in str(row.payload)
    sink=AsyncMock(return_value=0)
    assert (await ncm_events.deliver_due(db,sender=sink))['delivered']==0
    row=(await db.execute(text('SELECT * FROM ncm_notification_outbox'))).first()
    assert row.status=='pending' and row.attempts==1
    await db.execute(text('UPDATE ncm_notification_outbox SET next_attempt_at=now()'))
    await db.commit()
    sink.return_value=1
    assert (await ncm_events.deliver_due(db,sender=sink))['delivered']==1
    assert (await ncm_events.deliver_due(db,sender=sink))['processed']==0
    assert sink.await_count==2


@pytest.mark.asyncio
async def test_failed_capture_keeps_previous_artifact(env,monkeypatch):
    db,_,actor=env
    did=await device(db)
    prior=await capture(db,did,1,actor=actor,validated=True)
    def fail(*args): raise ncm.CaptureError('Incomplete capture: device prompt did not return')
    monkeypatch.setattr(ncm,'_netmiko_fetch',fail)
    with pytest.raises(ncm._FetchError): await ncm._do_fetch(db,did,actor=actor)
    assert (await db.execute(text('SELECT id FROM device_configs WHERE device_id=:id'),{'id':did})).scalar()==prior
    assert (await db.execute(text('SELECT status FROM ncm_backup_runs'))).scalar()=='failed'
    assert (await db.execute(text('SELECT last_status FROM device_ncm'))).scalar()=='failed'


@pytest.mark.asyncio
async def test_success_run_refreshes_coverage_without_new_snapshot(env,monkeypatch):
    db,_,actor=env
    did=await device(db)
    monkeypatch.setattr(ncm,'_netmiko_fetch',lambda *a:('cisco_ios','hostname edge\nend\n'))
    first=await ncm._do_fetch(db,did,actor=actor)
    again=await ncm._do_fetch(db,did,actor=actor)
    assert first['version_id']==again['version_id'] and again['deduplicated']
    overview=await ncm.ncm_overview(db,actor)
    assert overview['fresh']==1 and overview['eligible']==1
    await db.execute(text("UPDATE device_ncm SET config_types='{running,startup}'"))
    await db.commit()
    assert (await ncm.ncm_overview(db,actor))['fresh']==0


@pytest.mark.asyncio
async def test_archive_migration_is_resumable_and_audit_append_only(env):
    db,_,_=env
    did=await device(db)
    raw='hostname legacy\r\nend\r\n'
    await db.execute(text("INSERT INTO device_configs(device_id,content,content_hash) VALUES(:id,:content,:hash)"),{'id':did,'content':raw,'hash':hashlib.sha256(raw.encode()).hexdigest()})
    await db.commit()
    assert await ncm_maintenance.encrypt_legacy(db)==1
    assert await ncm_maintenance.encrypt_legacy(db)==0
    row=(await db.execute(text('SELECT * FROM device_configs'))).first()
    assert row.content=='' and read_content(row)==raw and not row.validated
    with pytest.raises(Exception,match='append-only'):
        async with db.begin_nested():
            await db.execute(text("DELETE FROM ncm_audit"))


@pytest.mark.asyncio
async def test_device_lock_prevents_overlapping_capture(env):
    db,sessions,actor=env
    did=await device(db)
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:device,0))'),{'device':str(did)})
    async with sessions() as other:
        with pytest.raises(ncm._FetchError,match='already running'): await ncm._do_fetch(other,did,actor=actor)


@pytest.mark.asyncio
async def test_scope_and_settings_preserve_omitted_fields(env):
    db,_,actor=env
    visible=await device(db,'a'); hidden=await device(db,'b')
    actor.role='operator'; actor.scope_tags=['A']
    await ncm._device_access(db,actor,visible)
    with pytest.raises(HTTPException) as error: await ncm._device_access(db,actor,hidden)
    assert error.value.status_code==404
    assert len((await ncm.ncm_overview(db,actor))['data'])==1
    assert len((await ncm.list_credentials(db,actor))['data'])==1
    with pytest.raises(HTTPException): await ncm._bulk_access(db,actor,[visible,hidden])
    await db.execute(text('UPDATE device_ncm SET freshness_hours=48 WHERE device_id=:id'),{'id':visible})
    await ncm._assurance_settings(db,actor,visible,ncm.NcmEnroll())
    assert (await db.execute(text('SELECT freshness_hours FROM device_ncm WHERE device_id=:id'),{'id':visible})).scalar()==48


@pytest.mark.asyncio
async def test_http_permissions_and_raw_export(env):
    db,_,actor=env
    did=await device(db)
    version=await capture(db,did,1,actor=actor,validated=True)
    app=FastAPI(); app.include_router(ncm.router); app.include_router(ncm.device_router)
    async def database(): yield db
    app.dependency_overrides[get_db]=database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
        assert (await client.post('/ncm/run-scheduled')).status_code==401
        app.dependency_overrides[security.get_current_user]=lambda:actor
        actor.role='devices'
        assert (await client.get('/ncm/overview')).status_code==403
        actor.role='viewer'
        assert (await client.post('/ncm/run-scheduled')).status_code==403
        route=f'/devices/{did}/configs/{version}'
        response=await client.get(route)
        assert response.status_code==200 and 'fixture1' not in response.text
        assert (await client.get(route+'?raw=true')).status_code==403
        actor.role='operator'
        assert (await client.post('/ncm/credentials',json={'name':'test','username':'test'})).status_code==403
        actor.role='admin'
        assert 'fixture1' in (await client.get(route+'?raw=true')).text


@pytest.mark.asyncio
async def test_job_dedup_restart_lease_and_retry(env,monkeypatch):
    from app.services import ncm_jobs
    db,sessions,actor=env
    did=await device(db)
    first=await ncm_jobs.enqueue(db,did,actor=actor)
    duplicate=await ncm_jobs.enqueue(db,did,actor=actor)
    await db.commit()
    assert first['job_id']==duplicate['job_id'] and not duplicate['queued']
    job=await ncm_jobs.claim(db)
    assert job.status=='running' and job.attempts==1
    assert await ncm_jobs.claim(db) is None
    await db.execute(text("UPDATE ncm_jobs SET lease_until=now()-interval '1 minute'"))
    await db.commit()
    recovered=await ncm_jobs.claim(db)
    assert recovered.id==job.id and recovered.lease_token!=job.lease_token and recovered.attempts==2
    monkeypatch.setattr(ncm,'_netmiko_fetch',lambda *a:('cisco_ios','hostname edge\nend\n'))
    await ncm_jobs.execute(sessions,recovered)
    row=(await db.execute(text('SELECT status,results FROM ncm_jobs'))).first()
    assert row.status=='success' and len(row.results)==1


@pytest.mark.asyncio
async def test_failed_jobs_retry_then_finish_and_cancel_queue(env,monkeypatch):
    from app.services import ncm_jobs
    db,sessions,actor=env
    did=await device(db)
    queued=await ncm_jobs.enqueue(db,did,actor=actor)
    await db.commit()
    def fail(*a): raise ncm.CaptureError('Device rejected the configuration command')
    monkeypatch.setattr(ncm,'_netmiko_fetch',fail)
    for attempt in range(1,4):
        job=await ncm_jobs.claim(db)
        assert job.attempts==attempt
        await ncm_jobs.execute(sessions,job)
        row=(await db.execute(text('SELECT status FROM ncm_jobs'))).first()
        assert row.status==('failed' if attempt==3 else 'queued')
        await db.execute(text('UPDATE ncm_jobs SET available_at=now()'))
        await db.commit()
    assert await ncm_jobs.claim(db) is None
    next_job=await ncm_jobs.enqueue(db,did,actor=actor)
    await db.commit()
    result=await ncm.cancel_backup(did,UUID(next_job['job_id']),db,actor)
    assert result['status']=='cancelled' and await ncm_jobs.claim(db) is None


@pytest.mark.asyncio
async def test_baseline_revisions_remain_protected(env):
    db,_,actor=env
    did=await device(db)
    manual=await capture(db,did,0,actor=actor)
    with pytest.raises(HTTPException) as error:
        await ncm.approve_baseline(did,ncm.BaselineIn(version_id=manual,name='manual',reason='not validated'),db,actor)
    assert error.value.status_code==422
    await db.rollback()
    first=await capture(db,did,1,actor=actor,validated=True)
    await ncm.approve_baseline(did,ncm.BaselineIn(version_id=first,name='first',reason='lab review'),db,actor)
    second=await capture(db,did,2,actor=actor,validated=True)
    await ncm.approve_baseline(did,ncm.BaselineIn(version_id=second,name='second',reason='lab review'),db,actor)
    for n in range(3,9): await capture(db,did,n,actor=actor,validated=True)
    rows=(await ncm.baselines(did,db,actor))['data']
    assert len(rows)==2 and sum(r['retired_at'] is None for r in rows)==1
    assert (await db.execute(text('SELECT count(*) FROM device_configs WHERE id IN (:a,:b)'),{'a':first,'b':second})).scalar()==2
    with pytest.raises(HTTPException) as error: await ncm.pin_config(did,first,ncm.PinIn(pinned=False),db,actor)
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_scheduler_scopes_and_enqueues_only_once(env):
    db,_,actor=env
    a=await device(db,'a'); b=await device(db,'b')
    await db.execute(text("UPDATE device_ncm SET schedule_enabled=true,schedule_timezone='Asia/Riyadh'"))
    await db.commit()
    actor.role='operator'; actor.scope_tags=['a']
    result=await ncm._run_scheduled(db,actor)
    assert result=={'due':1,'queued':1,'already_active':0}
    assert (await ncm._run_scheduled(db,actor))['due']==0
    assert (await db.execute(text('SELECT device_id FROM ncm_jobs'))).scalar()==a


@pytest.mark.asyncio
async def test_cross_device_compare_respects_both_scopes(env):
    db,_,actor=env
    a=await device(db,'a'); b=await device(db,'b')
    va=await capture(db,a,1,actor=actor); vb=await capture(db,b,2,actor=actor)
    result=await ncm.compare_versions(a,va,b,vb,False,True,db,actor)
    assert not result['identical'] and result['sensitive_changed']
    actor.role='operator'; actor.scope_tags=['a']
    with pytest.raises(HTTPException) as error: await ncm.compare_versions(a,va,b,vb,False,True,db,actor)
    assert error.value.status_code==404


@pytest.mark.asyncio
async def test_manual_import_cannot_spoof_validated_ssh(env):
    db,_,actor=env
    did=await device(db)
    result=await ncm.capture_config(did,ncm.ConfigCapture(content='hostname pasted\nend\n',captured_by='ssh'),db,actor)
    row=(await db.execute(text('SELECT captured_by,validated,actor_id FROM device_configs WHERE id=:id'),{'id':UUID(result['version_id'])})).first()
    assert row.captured_by=='manual' and not row.validated and row.actor_id==actor.id
    assert (await ncm.ncm_overview(db,actor))['fresh']==0


@pytest.mark.asyncio
async def test_notification_terminal_failure_and_disabled_channel(env):
    db,_,actor=env
    did=await device(db)
    channel=(await db.execute(text("INSERT INTO notification_channels(type,config,enabled) VALUES('webhook','{}',true) RETURNING id"))).scalar()
    await db.execute(text('UPDATE device_ncm SET notify_channels=:ids WHERE device_id=:id'),{'ids':[channel],'id':did})
    await db.commit()
    await capture(db,did,1,actor=actor); await capture(db,did,2,actor=actor)
    await db.execute(text('UPDATE ncm_notification_outbox SET attempts=7'))
    await db.commit()
    await ncm_events.deliver_due(db,sender=AsyncMock(return_value=0))
    assert (await db.execute(text('SELECT status FROM ncm_notification_outbox'))).scalar()=='failed'
    await capture(db,did,3,actor=actor)
    await db.execute(text('UPDATE notification_channels SET enabled=false'))
    await db.commit()
    sink=AsyncMock(return_value=1)
    await ncm_events.deliver_due(db,sender=sink)
    sink.assert_not_awaited()
    assert (await db.execute(text("SELECT count(*) FROM ncm_notification_outbox WHERE status='disabled'"))).scalar()==1
