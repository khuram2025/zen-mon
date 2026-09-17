"""Opt-in PostgreSQL/ClickHouse contracts. Never run against application databases.

The runner must supply an isolated Unix-socket PostgreSQL cluster and a loopback
ClickHouse listener. Only outbound notification delivery is replaced with a sink.
"""
import os
import pytest

if os.environ.get('ZENPLUS_NETWORK_INTEGRATION') != '1':
    pytest.skip('Requires disposable network integration databases', allow_module_level=True)

from pathlib import Path
from urllib.parse import urlsplit, parse_qs

url = urlsplit(os.environ['DATABASE_URL'])
socket = Path(parse_qs(url.query)['host'][0]).resolve()
assert url.username == 'network_fixture' and url.port == 15432
assert socket.parent.name.startswith('zenplus-network-integration-') and socket.parent.parent == Path('/tmp')
assert os.environ['CLICKHOUSE_HOST'] == '127.0.0.1' and os.environ['CLICKHOUSE_HTTP_PORT'] == '18123'
assert os.environ['CLICKHOUSE_USER'] == 'network_fixture'

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from app.core.database import get_clickhouse_client
from app.models.user import User
from app.models.device import Device
from app.models.discovery_v2 import DiscoveryProfile, DiscoveryRun, DiscoveryResultV2
from app.api.v1 import alert_rules, discovery_v2, network_events as events_api
from app.schemas.discovery_v2 import ImportRequest
from app.services.network_events import parse_syslog, ingest_syslog
from app.services import network_alert_service as worker


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine(os.environ['DATABASE_URL'], poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await db.execute(text('TRUNCATE devices, users, discovery_profiles, alert_rules CASCADE'))
        user = User(username='network-fixture', email='fixture@example.invalid', password_hash='unusable', role='admin')
        db.add(user)
        await db.commit()
        ch = get_clickhouse_client()
        ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
        ch.command('TRUNCATE TABLE zenplus.snmp_if_metrics')
        sink = AsyncMock(return_value=1)
        monkeypatch.setattr(worker, 'dispatch_to_channels', sink)
        from app.services import host_alert_service
        monkeypatch.setattr(host_alert_service, 'dispatch_to_channels', sink)
        yield db, sessions, user, ch, sink
    await engine.dispose()


async def node(db, ip='192.0.2.10', **kw):
    item = Device(hostname='fixture', ip_address=ip, snmp_enabled=True, status='up', **kw)
    db.add(item)
    await db.commit()
    return item


async def rule(db, user, metric='cpu', **kw):
    data = dict(name=f'fixture-{uuid4()}', metric=metric, operator='>', threshold=90, notify_channels=[])
    data.update(kw)
    return await alert_rules.create_alert_rule(alert_rules.AlertRuleCreate(**data), db, user)


async def incidents(db):
    return (await db.execute(text('SELECT * FROM alerts ORDER BY triggered_at'))).mappings().all()


def samples(ch, device, series):
    now = datetime.now(timezone.utc)
    rows = [[device.id, metric, value, '', now + timedelta(seconds=offset), 'integration-fixture']
            for metric, values in series.items() for offset, value in values]
    ch.insert('snmp_metrics', rows, column_names=['device_id', 'metric_key', 'value', 'unit', 'timestamp', 'poller_id'])


@pytest.mark.asyncio
async def test_compound_conditions_hold_unknown_and_recovery(env):
    db, _, user, ch, sink = env
    device = await node(db)
    created = await rule(db, user, conditions=[dict(metric='cpu', operator='>', threshold=90, reset_threshold=50),
        dict(metric='memory', operator='>', threshold=80, reset_threshold=50)], min_duration=60, recovery_alert=True)
    samples(ch, device, {'cpu': [(-10, 99)], 'memory': [(-10, 95)]})
    assert (await worker.evaluate_network_rules(db))['raised'] == 0
    ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
    samples(ch, device, {'cpu': [(-90, 99), (-30, 99)], 'memory': [(-90, 95), (-30, 95)]})
    assert (await worker.evaluate_network_rules(db))['raised'] == 1
    assert len(await incidents(db)) == 1
    ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
    await worker.evaluate_network_rules(db)
    assert (await incidents(db))[0]['status'] == 'active'
    samples(ch, device, {'cpu': [(-10, 70)], 'memory': [(-10, 70)]})
    await worker.evaluate_network_rules(db)
    assert (await incidents(db))[0]['status'] == 'active'
    ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
    samples(ch, device, {'cpu': [(-90, 10), (-30, 10)], 'memory': [(-90, 10), (-30, 10)]})
    await worker.evaluate_network_rules(db)
    assert (await incidents(db))[0]['status'] == 'resolved'
    await alert_rules.update_alert_rule(created['id'], alert_rules.AlertRuleUpdate(condition_logic='OR'), db, user)
    ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
    samples(ch, device, {'cpu': [(-90, 99), (-30, 99)], 'memory': [(-90, 10), (-30, 10)]})
    await worker.evaluate_network_rules(db)
    assert len(await incidents(db)) == 2


@pytest.mark.asyncio
async def test_template_components_create_independent_incidents(env):
    db, _, user, ch, _ = env
    device = await node(db)
    await rule(db, user, 'tpl_tunnel', operator='==', threshold=2)
    samples(ch, device, {'tpl_tunnel_1': [(-10, 2)], 'tpl_tunnel_2': [(-10, 1)]})
    await worker.evaluate_network_rules(db)
    found = await incidents(db)
    assert len(found) == 1 and found[0]['metadata']['if_index'] == 'series:tpl_tunnel_1'


@pytest.mark.asyncio
async def test_syslog_concurrent_replay_cooldown_scope_and_maintenance(env):
    db, sessions, user, _, sink = env
    device = await node(db, tags=['Team-A'])
    await rule(db, user, 'syslog', operator='<=', threshold=4, target='link')
    event = parse_syslog(b'<34>Sep 10 12:00:00 untrusted link down', str(device.ip_address))
    async def ingest():
        async with sessions() as session:
            return await ingest_syslog(session, event, sink)
    results = await asyncio.wait_for(asyncio.gather(ingest(), ingest()), 10)
    assert sum(r['alerts_created'] for r in results) == 1
    assert sum(bool(r.get('replayed')) for r in results) == 1
    assert sink.await_count == 1
    other = {**event, 'id': str(uuid4())}
    assert (await ingest_syslog(db, other, sink))['alerts_created'] == 0
    user.role = 'viewer'; user.scope_tags = ['team-a']
    assert len((await events_api.list_events(24, '', 7, 200, db, user))['data']) == 2
    user.scope_tags = ['team-b']
    assert (await events_api.list_events(24, '', 7, 200, db, user))['data'] == []
    device.status = 'maintenance'
    await db.commit()
    assert (await ingest_syslog(db, {**event, 'id': str(uuid4())}, sink))['suppressed'] is True
    assert sink.await_count == 1


@pytest.mark.asyncio
async def test_discovery_concurrent_import_and_policy_preservation(env):
    db, sessions, user, _, _ = env
    profile = DiscoveryProfile(name='fixture')
    db.add(profile); await db.flush()
    runs = [DiscoveryRun(profile_id=profile.id, trigger_type='manual') for _ in range(2)]
    db.add_all(runs); await db.flush()
    results = [DiscoveryResultV2(run_id=r.id, profile_id=profile.id, ip_address='192.0.2.20',
                               hostname='discovered', device_type='switch') for r in runs]
    db.add_all(results); await db.commit()
    async def do_import(i):
        async with sessions() as session:
            return await discovery_v2.import_results(runs[i].id,
                ImportRequest(result_ids=[results[i].id], conflict_strategy='update'), session, user)
    outcomes = await asyncio.wait_for(asyncio.gather(do_import(0), do_import(1)), 10)
    assert sum(o.devices_created for o in outcomes) == 1
    assert all(o.failed == 0 for o in outcomes)
    device = (await db.execute(text("SELECT id FROM devices WHERE ip_address='192.0.2.20'"))).scalar_one()
    await db.execute(text("UPDATE devices SET ping_enabled=false, tags='[\"operator\"]', snmp_poll_interval=240 WHERE id=:id"), {'id': device})
    results[0].imported = False
    await db.commit()
    await do_import(0)
    policy = (await db.execute(text('SELECT ping_enabled,tags,snmp_poll_interval FROM devices WHERE id=:id'), {'id': device})).one()
    assert tuple(policy) == (False, ['operator'], 240)


@pytest.mark.asyncio
async def test_ipv6_trap_storage_migration_preserves_identity(env):
    _, _, _, ch, _ = env
    event_id = uuid4()
    ch.insert('snmp_traps', [[None, '0.0.0.0', '2001:db8::10', event_id, '1.3.6.1.6.3.1.1.5.3',
        'linkDown', '{}', 'warning', 'fixture', datetime.now(timezone.utc), 'fixture']],
        column_names=['device_id','source_ip','source_ip_text','event_id','trap_oid','trap_name','bindings',
                      'severity','message','timestamp','poller_id'])
    found = ch.query('SELECT source_ip_text, event_id FROM snmp_traps WHERE event_id={id:UUID}',
                     parameters={'id': str(event_id)}).first_row
    assert found == ('2001:db8::10', event_id)


@pytest.mark.asyncio
async def test_parent_outage_preserves_incident_and_recovery_clears_suppression(env):
    db, _, user, ch, _ = env
    parent = await node(db, '192.0.2.1')
    child = await node(db)
    await rule(db, user, device_id=child.id)
    samples(ch, child, {'cpu': [(-10, 99)]})
    await worker.evaluate_network_rules(db)
    await db.execute(text('INSERT INTO topology_dependencies (parent_device_id,child_device_id) VALUES (:p,:c)'),
                     {'p': parent.id, 'c': child.id})
    parent.status = 'down'; await db.commit()
    ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
    samples(ch, child, {'cpu': [(-10, 10)]})
    await worker.evaluate_network_rules(db)
    found = (await incidents(db))[0]
    assert found['status'] == 'active' and found['metadata']['suppressed_by_dependency'] is True
    parent.status = 'up'; await db.commit()
    await worker.evaluate_network_rules(db)
    found = (await incidents(db))[0]
    assert found['status'] == 'resolved' and found['metadata']['suppressed_by_dependency'] is False
    assert 'parent_device_id' not in found['metadata']


@pytest.mark.asyncio
async def test_trap_concurrent_uuid_replay_and_dependency_suppression(env):
    from app.api.v1 import alert_engine
    db, sessions, user, _, sink = env
    device = await node(db)
    await rule(db, user, 'trap', trap_oid='1.3.6.1.6.3.1.1.5.3', notify_channels=['fixture-sink'])
    event = alert_engine.TrapEvent(event_id=uuid4(), device_id=str(device.id), source_ip=str(device.ip_address),
                                   trap_oid='1.3.6.1.6.3.1.1.5.3', message='fixture link down')
    async def evaluate():
        async with sessions() as session:
            return await alert_engine.evaluate_trap(event, session)
    found = await asyncio.wait_for(asyncio.gather(evaluate(), evaluate()), 10)
    assert sum(r['alerts_created'] for r in found) == 1 and sink.await_count == 1
    assert len(await incidents(db)) == 1
    parent = await node(db, '192.0.2.1')
    parent.status = 'down'
    await db.execute(text('INSERT INTO topology_dependencies (parent_device_id,child_device_id) VALUES (:p,:c)'),
                     {'p': parent.id, 'c': device.id})
    await db.commit()
    event.event_id = uuid4()
    assert (await alert_engine.evaluate_trap(event, db))['suppressed'] == 'dependency'


@pytest.mark.asyncio
async def test_mib_upload_subprocess_compile_resolve_and_stale_index(env, monkeypatch, tmp_path):
    from io import BytesIO
    from fastapi import UploadFile, HTTPException
    from app.api.v1 import snmp
    db, _, user, _, _ = env
    await db.execute(text('TRUNCATE snmp_mibs'))
    monkeypatch.setattr(snmp, 'MIB_DIR', tmp_path)
    source = b'''FIXTURE-MIB DEFINITIONS ::= BEGIN
    IMPORTS enterprises FROM SNMPv2-SMI;
    fixtureRoot OBJECT IDENTIFIER ::= { enterprises 55555 }
    fixtureValue OBJECT IDENTIFIER ::= { fixtureRoot 1 }
    END'''
    await snmp.upload_mib(UploadFile(filename='fixture.mib', file=BytesIO(source)), 'fixture', None, db, user)
    compiled = await snmp.compile_mibs(db, user)
    assert compiled['errors'] == {} and compiled['object_count'] >= 2
    resolved = await snmp.mib_objects('', 'FIXTURE-MIB::fixtureValue', db, user)
    assert resolved['oid'] == '1.3.6.1.4.1.55555.1'
    await snmp.upload_mib(UploadFile(filename='fixture.mib', file=BytesIO(source.replace(b'55555', b'55556'))), 'fixture', None, db, user)
    with pytest.raises(HTTPException) as error:
        await snmp.mib_objects('', 'FIXTURE-MIB::fixtureValue', db, user)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_profile_export_import_preserves_collection_and_display_contract(env):
    from app.api.v1 import snmp
    from app.schemas.snmp import ProfileCreate
    db, _, user, _, _ = env
    data = ProfileCreate(name=f'fixture-{uuid4()}', vendor='Fixture',
        match_rules={'sys_object_id_prefixes': ['1.3.6.1.4.1.55555']},
        oid_groups=[{'key': 'health', 'name': 'Health', 'interval_seconds': 120,
            'metrics': [{'key': 'temperature', 'name': 'Temperature', 'oid': '1.3.6.1.4.1.55555.1.0',
                         'scale': 0.1, 'unit': 'C', 'thresholds': {'warn': 75, 'crit': 90}}]}])
    profile = await snmp.create_profile(data, db, user)
    bundle = await snmp.export_profile(profile.id, db, user)
    bundle['profile']['name'] += '-copy'
    imported = await snmp.import_profile(snmp.ProfileBundle(**bundle), db, user)
    assert imported.id != profile.id and imported.builtin is False
    assert imported.oid_groups == profile.oid_groups and imported.match_rules == profile.match_rules


@pytest.mark.asyncio
async def test_discovery_invalid_ids_and_failed_device_do_not_abort_valid_import(env):
    db, _, user, _, _ = env
    profile = DiscoveryProfile(name='fixture-failures'); db.add(profile); await db.flush()
    run = DiscoveryRun(profile_id=profile.id, trigger_type='manual'); db.add(run); await db.flush()
    results = [DiscoveryResultV2(run_id=run.id, profile_id=profile.id, ip_address=ip, device_type='switch',
                                matched_template_id=template) for ip, template in
               [('192.0.2.30', None), ('192.0.2.31', None)]]
    # Discovery's 60-character type exceeds the inventory's 50-character column.
    results[0].device_type = 'x' * 51
    db.add_all(results); await db.commit()
    valid = await discovery_v2.import_results(run.id, ImportRequest(result_ids=[-1, results[0].id, results[1].id]), db, user)
    assert valid.failed == 2 and valid.devices_created == 1
    assert (await db.execute(text('SELECT count(*) FROM devices'))).scalar_one() == 1


@pytest.mark.asyncio
async def test_scoped_http_event_read_and_retention_boundary(env):
    import httpx
    from app.main import app
    from app.core.database import get_db
    from app.core.security import get_current_user
    db, sessions, user, _, sink = env
    await node(db, tags=['Team-A'])
    current = parse_syslog(b'<34>fixture current', '192.0.2.10')
    old = {**current, 'id': str(uuid4()), 'received_at': datetime.now(timezone.utc)-timedelta(days=31)}
    await ingest_syslog(db, old, sink); await ingest_syslog(db, current, sink)
    await db.execute(text('DELETE FROM network_events WHERE received_at < now() - make_interval(days => :days)'), {'days': 30})
    await db.commit()
    user.role = 'viewer'; user.scope_tags = ['team-a']
    async def session_override():
        async with sessions() as session: yield session
    app.dependency_overrides[get_db] = session_override
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            response = await client.get('/api/v1/network-events')
            assert response.status_code == 200
            assert [e['id'] for e in response.json()['data']] == [current['id']]
            user.scope_tags = ['team-b']
            assert (await client.get('/api/v1/network-events')).json()['data'] == []
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['device', 'group', 'tag', 'all', 'manual'])
async def test_maintenance_consistently_suppresses_metrics_syslog_and_traps(env, mode):
    from app.models.device import DeviceGroup, DeviceMaintenance
    from app.api.v1 import alert_engine
    db, _, user, ch, sink = env
    group = DeviceGroup(name='maintenance-fixture'); db.add(group); await db.commit()
    device = await node(db, tags=['team-a'], group_id=group.id)
    if mode == 'manual':
        device.status = 'maintenance'
    else:
        now = datetime.now(timezone.utc)
        db.add(DeviceMaintenance(scope_type=mode, scope_device_id=device.id if mode=='device' else None,
            scope_group_id=group.id if mode=='group' else None, scope_tag='team-a' if mode=='tag' else None,
            starts_at=now-timedelta(minutes=1), ends_at=now+timedelta(minutes=5)))
    await db.commit()
    await rule(db, user)
    await rule(db, user, 'syslog', operator='<=', threshold=4)
    await rule(db, user, 'trap', notify_channels=['fixture-sink'])
    samples(ch, device, {'cpu': [(-10, 99)]})
    await worker.evaluate_network_rules(db)
    syslog = parse_syslog(b'<34>fixture maintenance', str(device.ip_address))
    assert (await ingest_syslog(db, syslog, sink)).get('suppressed') is True
    trap = alert_engine.TrapEvent(event_id=uuid4(), device_id=str(device.id), source_ip=str(device.ip_address), trap_oid='1.2.3')
    assert (await alert_engine.evaluate_trap(trap, db)).get('suppressed') == 'maintenance'
    assert await incidents(db) == [] and sink.await_count == 0


@pytest.mark.asyncio
async def test_concurrent_network_evaluators_create_one_incident(env):
    db, sessions, user, ch, _ = env
    device = await node(db)
    await rule(db, user)
    samples(ch, device, {'cpu': [(-10, 99)]})
    async def evaluate():
        async with sessions() as session:
            return await worker.evaluate_network_rules(session)
    outcomes = await asyncio.wait_for(asyncio.gather(evaluate(), evaluate()), 10)
    assert sum(o['raised'] for o in outcomes) == 1
    assert len(await incidents(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cycle', range(3))
async def test_simulated_device_discovery_poll_alert_recovery_campaign(env, monkeypatch, cycle):
    """Real loopback SNMP GET, discovery executor/import, SQL history and alert lifecycle."""
    from sqlalchemy import select
    from pyasn1.type import univ
    from app.services import discovery_executor
    from app.api.v1.snmp import _snmpget_detail
    from tests.integration.snmp_simulator import SimulatedSNMP
    db, sessions, user, ch, sink = env
    loop=asyncio.get_running_loop()
    transport, simulator=await loop.create_datagram_endpoint(SimulatedSNMP,local_addr=('127.0.0.1',0))
    port=transport.get_extra_info('sockname')[1]
    monkeypatch.setattr(discovery_executor,'AsyncSessionLocal',sessions)
    monkeypatch.setattr(discovery_executor,'_load_snmp_credentials',AsyncMock(return_value=[
        dict(snmp_version='2c',community=simulator.community,port=port)]))
    profile=DiscoveryProfile(name=f'campaign-{cycle}', targets=['127.0.0.1'], protocols=['snmp'], scan_timeout_ms=1000)
    db.add(profile); await db.flush()
    run=DiscoveryRun(profile_id=profile.id,trigger_type='manual'); db.add(run); await db.commit()
    try:
        await asyncio.wait_for(discovery_executor.execute_run(run.id),15)
        await db.refresh(run)
        assert run.status=='completed', run.error_details
        result=(await db.execute(select(DiscoveryResultV2).where(DiscoveryResultV2.run_id==run.id))).scalar_one()
        assert result.sys_name=='simulated-network-device' and result.import_ready
        imported=await discovery_v2.import_results(run.id,ImportRequest(result_ids=[result.id]),db,user)
        assert imported.devices_created==1 and imported.failed==0
        await db.refresh(result)
        device=await db.get(Device,result.imported_device_id)
        # Repeat import against operator-owned monitoring settings.
        device.ping_enabled=False; device.snmp_poll_interval=240; device.tags=['campaign-policy']
        result.imported=False; await db.commit()
        again=await discovery_v2.import_results(run.id,ImportRequest(result_ids=[result.id],conflict_strategy='update'),db,user)
        assert again.devices_created==0 and again.failed==0
        await db.refresh(device)
        assert not device.ping_enabled and device.snmp_poll_interval==240 and device.tags==['campaign-policy']
        # The simulator credential is supplied in memory, not registered in
        # the credential vault, so explicitly enable monitoring for its sample stream.
        device.snmp_enabled=True
        await db.commit()
        await rule(db,user,device_id=device.id,reset_threshold=50,recovery_alert=True)
        oid='1.3.6.1.4.1.2021.11.11.0'
        for value,expected in [(99,'active'),(10,'resolved')]:
            simulator.values[oid]=univ.Integer(value)
            measured,error=await _snmpget_detail(ip='127.0.0.1',community=simulator.community,
                version='2c',port=port,timeout_ms=1000,oids=[oid])
            assert not error and float(measured[oid])==value
            ch.command('TRUNCATE TABLE zenplus.snmp_metrics')
            samples(ch,device,{'cpu':[(-5,float(measured[oid]))]})
            await worker.evaluate_network_rules(db)
            found=await incidents(db)
            assert len(found)==1 and found[0]['status']==expected
        assert simulator.requests>=3
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_receiver_udp_persistence_alert_resolution_idle_retention_and_restart(env,tmp_path):
    from app.services.syslog_receiver import Service,Config
    db,sessions,user,_,sink=env
    device=await node(db,ip='127.0.0.1')
    await rule(db,user,'syslog',device_id=device.id,operator='<=',threshold=4,target='campaign-event')
    cfg=Config(port=0,retention_interval=.05,status_interval=.02,status_path=str(tmp_path/'status.json'))
    from app.api.v1 import alerts
    for cycle in range(2):
        service=Service(cfg,sessions); stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
        await asyncio.wait_for(service.ready.wait(),2)
        address=service.transport.get_extra_info('sockname')
        sender,_=await asyncio.get_running_loop().create_datagram_endpoint(asyncio.DatagramProtocol,remote_addr=address)
        try:
            sender.sendto(b'<34>campaign-event')
            async def received():
                while service.counters.processed<1: await asyncio.sleep(.01)
            await asyncio.wait_for(received(),3)
            found=await incidents(db)
            assert len(found)==1  # restart also observes the persisted cooldown
            if cycle==0:
                await alerts.acknowledge_alert(found[0]['id'],db,user)
                await alerts.resolve_alert(found[0]['id'],db,user)
            assert (await incidents(db))[0]['status']=='resolved'
            old=parse_syslog(b'<34>old-fixture','192.0.2.8')
            old['received_at']-=timedelta(days=31)
            await ingest_syslog(db,old,sink)
            async def expired():
                while (await db.execute(text('SELECT 1 FROM network_events WHERE id=:id'),{'id':old['id']})).first():
                    await asyncio.sleep(.02)
            await asyncio.wait_for(expired(),3)
            assert service.counters.retention_runs>0
        finally:
            sender.close(); stop.set(); await asyncio.wait_for(task,3)
        assert service.counters.shutdown_abandoned==0
        cfg=Config(port=address[1],retention_interval=.05,status_path='')


@pytest.mark.asyncio
async def test_receiver_process_sigterm_drains_and_restarts(env,tmp_path):
    import json,signal,socket,sys
    db,_,_,_,_=env
    probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);probe.bind(('127.0.0.1',0))
    port=probe.getsockname()[1];probe.close()
    status=tmp_path/'process-status.json'
    config={**os.environ,'SYSLOG_HOST':'127.0.0.1','SYSLOG_PORT':str(port),
            'SYSLOG_STATUS_PATH':str(status),'SYSLOG_STATUS_INTERVAL':'.02','SYSLOG_SHUTDOWN_TIMEOUT':'2'}
    async def wait_status(predicate):
        async def check():
            while True:
                if status.exists():
                    value=json.loads(status.read_text())
                    if predicate(value): return value
                await asyncio.sleep(.01)
        return await asyncio.wait_for(check(),5)
    for cycle in range(2):
        with (tmp_path/f'process-{cycle}.log').open('wb') as log:
            proc=await asyncio.create_subprocess_exec(sys.executable,'-m','app.services.syslog_receiver',
                cwd=str(Path(__file__).resolve().parents[2]),env=config,stdout=log,stderr=log)
            try:
                await wait_status(lambda value:value['pid']==proc.pid and value['running'])
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                    sender.sendto(f'<134>process-campaign-{cycle}'.encode(),('127.0.0.1',port))
                await wait_status(lambda value:value['pid']==proc.pid and value['counters']['processed']==1)
                proc.send_signal(signal.SIGTERM)
                assert await asyncio.wait_for(proc.wait(),5)==0
                final=json.loads(status.read_text())
                assert not final['running'] and final['counters']['shutdown_abandoned']==0
            finally:
                if proc.returncode is None: proc.kill(); await proc.wait()
    total=(await db.execute(text("SELECT count(*) FROM network_events WHERE message LIKE 'process-campaign-%'"))).scalar_one()
    assert total==2
