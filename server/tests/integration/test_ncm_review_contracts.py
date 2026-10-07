"""NCCM development-review contracts on a disposable PostgreSQL fixture."""
import os
import pytest
if os.environ.get('ZENPLUS_NCM_INTEGRATION') != '1':
    pytest.skip('Requires isolated NCM PostgreSQL fixture', allow_module_level=True)

import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

from fastapi import HTTPException
from paramiko import AuthenticationException, Ed25519Key
from sqlalchemy import text

from app.api.v1 import ncm, ncm_compliance as compliance_api
from app.services import ncm_events, ncm_jobs, ncm_compliance, ncm_changes
from app.services import ncm_readiness
from tests.integration.test_ncm_contracts import env, device, capture  # noqa: F401  (fixtures)

GOOD = 'hostname edge\nip ssh version 2\nsnmp-server community public RO\nline vty 0 4\n transport input ssh\nend\n'


@pytest.fixture(autouse=True)
def healthy_runtime(monkeypatch):
    monkeypatch.setattr(ncm_readiness, 'runtime_health',
                        lambda: dict(worker='active', scheduler='active', delivery='active', encryption='configured',
                                     time_sync='synchronized', server_time=datetime.now(timezone.utc).isoformat()))


async def run_job(db, sessions):
    job = await ncm_jobs.claim(db)
    assert job
    await ncm_jobs.execute(sessions, job)
    return (await db.execute(text('SELECT * FROM ncm_jobs WHERE id=:id'), {'id': job.id})).mappings().first()


# --- B02 ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_queue_enforces_readiness_without_creating_jobs(env, monkeypatch):
    db, _, actor = env
    did = await device(db)
    monkeypatch.setattr(ncm_readiness, 'runtime_health',
                        lambda: dict(worker='inactive', scheduler='active', delivery='active', encryption='configured',
                                     time_sync='synchronized', server_time=datetime.now(timezone.utc).isoformat()))
    with pytest.raises(HTTPException) as blocked:
        await ncm.queue_backup(did, db, actor)
    assert blocked.value.status_code == 409 and blocked.value.detail['code'] == 'readiness_blocked'
    assert [b['id'] for b in blocked.value.detail['blockers']] == ['worker']
    await db.rollback()
    assert (await db.execute(text('SELECT count(*) FROM ncm_jobs'))).scalar() == 0
    monkeypatch.undo()
    monkeypatch.setattr(ncm_readiness, 'runtime_health',
                        lambda: dict(worker='active', scheduler='active', delivery='active', encryption='configured',
                                     time_sync='synchronized', server_time=datetime.now(timezone.utc).isoformat()))
    assert (await ncm.queue_backup(did, db, actor))['queued']


@pytest.mark.asyncio
async def test_backup_connects_without_any_saved_host_key(env, monkeypatch):
    db, sessions, actor = env
    did = await device(db)
    await ncm_jobs.enqueue(db, did, actor=actor)
    await db.commit()
    seen = {}
    def fetch(*args, **kwargs):
        seen.update(kwargs)
        return 'cisco_ios', GOOD
    monkeypatch.setattr(ncm, '_netmiko_fetch', fetch)
    job = await run_job(db, sessions)
    assert job['status'] == 'success' and job['attempts'] == 1
    assert 'known_hosts' not in seen


# --- B03 / B07 ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bulk_settings_change_only_supplied_fields(env):
    db, _, actor = env
    off = await device(db); on = await device(db)
    await db.execute(text("""UPDATE device_ncm SET enabled=false,schedule_enabled=true,schedule_type='daily',
        schedule_time='02:00',schedule_timezone='Asia/Riyadh',keep_versions=7 WHERE device_id=:id"""), {'id': off})
    await db.execute(text("UPDATE device_ncm SET schedule_timezone='UTC' WHERE device_id=:id"), {'id': on})
    await db.commit()
    preview = await ncm.bulk_backup_settings(ncm.BulkBackupSettings(device_ids=[off, on], keep_versions=9, dry_run=True), db, actor)
    assert preview['dry_run'] and preview['will_update'] == 2
    first = next(r for r in preview['results'] if r['device_id'] == str(off))
    assert list(first['changes']) == ['keep_versions'] and first['timezone'] == 'Asia/Riyadh'
    assert first['next_run_at'] and first['schedule_summary'] == 'Daily at 02:00 Asia/Riyadh'
    assert (await db.execute(text('SELECT keep_versions FROM device_ncm WHERE device_id=:id'), {'id': off})).scalar() == 7
    await ncm.bulk_backup_settings(ncm.BulkBackupSettings(device_ids=[off, on], keep_versions=9), db, actor)
    rows = {r.device_id: r for r in (await db.execute(text('SELECT * FROM device_ncm'))).fetchall()}
    assert not rows[off].enabled and rows[on].enabled                  # collection state preserved
    assert rows[off].schedule_type == 'daily' and rows[off].schedule_timezone == 'Asia/Riyadh'
    assert rows[off].keep_versions == 9 and rows[on].keep_versions == 9


@pytest.mark.asyncio
async def test_bulk_settings_reject_invalid_merged_state_atomically(env):
    db, _, actor = env
    a = await device(db); b = await device(db)
    with pytest.raises(HTTPException) as error:
        await ncm.bulk_backup_settings(ncm.BulkBackupSettings(device_ids=[a, b], schedule_enabled=True, schedule_type='weekly',
                                                              schedule_time='03:00', keep_versions=4), db, actor)
    assert error.value.status_code == 422 and error.value.detail['invalid'] == 2
    assert (await db.execute(text('SELECT count(*) FROM device_ncm WHERE keep_versions=4'))).scalar() == 0
    result = await ncm.bulk_backup_settings(ncm.BulkBackupSettings(device_ids=[a], enabled=False), db, actor)
    assert result['updated'] == 1
    assert not (await db.execute(text('SELECT enabled FROM device_ncm WHERE device_id=:id'), {'id': a})).scalar()


@pytest.mark.asyncio
async def test_bulk_assign_keeps_collection_state(env):
    db, _, actor = env
    did = await device(db)
    await db.execute(text('UPDATE device_ncm SET enabled=false'))
    cred = (await db.execute(text("INSERT INTO ncm_credentials(name,username) VALUES('other','o') RETURNING id"))).scalar()
    await db.commit()
    await ncm.bulk_assign_profile(ncm.BulkAssign(device_ids=[did], credential_id=cred), db, actor)
    row = (await db.execute(text('SELECT enabled,credential_id FROM device_ncm'))).first()
    assert not row.enabled and row.credential_id == cred


# --- B04 / B14 / F13 -------------------------------------------------------------
@pytest.mark.asyncio
async def test_profile_in_use_needs_replacement(env):
    db, _, actor = env
    did = await device(db)
    cred = (await db.execute(text('SELECT credential_id FROM device_ncm WHERE device_id=:id'), {'id': did})).scalar()
    spare = (await db.execute(text("INSERT INTO ncm_credentials(name,username) VALUES('spare','s') RETURNING id"))).scalar()
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await ncm.delete_credential(cred, None, db, actor)
    assert error.value.status_code == 409 and error.value.detail['devices'][0]['device_id'] == str(did)
    await db.rollback()
    result = await ncm.delete_credential(cred, spare, db, actor)
    assert result == {'deleted': True, 'reassigned': 1}
    assert (await db.execute(text('SELECT credential_id FROM device_ncm'))).scalar() == spare
    assert (await ncm.delete_credential(UUID(str((await db.execute(text("INSERT INTO ncm_credentials(name,username) VALUES('x','x') RETURNING id"))).scalar())), None, db, actor))['reassigned'] == 0


@pytest.mark.asyncio
async def test_enable_secret_clear_and_key_profiles(env):
    db, _, actor = env
    base = dict(name='p', username='u', password='pw', enable_password='en')
    cid = UUID((await ncm.create_credential(ncm.CredentialIn(**base), db, actor))['id'])
    await ncm.update_credential(cid, ncm.CredentialIn(name='p2', username='u'), db, actor)
    row = (await db.execute(text('SELECT password_enc,enable_password_enc FROM ncm_credentials WHERE id=:id'), {'id': cid})).first()
    assert row.password_enc and row.enable_password_enc                   # blank keeps
    await ncm.update_credential(cid, ncm.CredentialIn(name='p2', username='u', clear_enable_password=True), db, actor)
    assert (await db.execute(text('SELECT enable_password_enc FROM ncm_credentials WHERE id=:id'), {'id': cid})).scalar() is None
    import io
    key = Ed25519Key.from_private_key(io.StringIO(_ed25519_pem()))
    kid = UUID((await ncm.create_credential(ncm.CredentialIn(name='k', username='u', auth_method='key', private_key=_ed25519_pem()), db, actor))['id'])
    listed = next(c for c in (await ncm.list_credentials(db, actor))['data'] if c['id'] == str(kid))
    assert listed['has_private_key'] and listed['key_type'] == 'ssh-ed25519' and listed['key_fingerprint'].startswith('SHA256:')
    assert _ed25519_pem() not in str(listed) and key.get_base64() not in str(listed)
    with pytest.raises(HTTPException) as bad:
        await ncm.create_credential(ncm.CredentialIn(name='bad', username='u', auth_method='key', private_key='not a key'), db, actor)
    assert bad.value.status_code == 422


_PEM = None


def _ed25519_pem():
    global _PEM
    if _PEM is None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        _PEM = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                          serialization.NoEncryption()).decode()
    return _PEM


# --- B05 / B06: diagnosable attempts and correlated incidents ---------------------
@pytest.mark.asyncio
async def test_permanent_failure_opens_one_incident_and_recovery_closes_it(env, monkeypatch):
    db, sessions, actor = env
    did = await device(db)
    channel = (await db.execute(text("INSERT INTO notification_channels(type,config,enabled,name) VALUES('webhook','{}',true,'ops') RETURNING id"))).scalar()
    await db.execute(text('UPDATE device_ncm SET notify_channels=:c'), {'c': [channel]})
    await db.commit()

    def denied(*a, **k):
        raise AuthenticationException('SECRET-DETAIL')
    monkeypatch.setattr(ncm, '_netmiko_fetch', denied)
    for _ in range(2):
        await ncm_jobs.enqueue(db, did, actor=actor); await db.commit()
        job = await run_job(db, sessions)
        assert job['status'] == 'failed' and job['attempts'] == 1 and job['failure_category'] == 'authentication'
    incidents = (await db.execute(text('SELECT * FROM ncm_incidents'))).fetchall()
    assert len(incidents) == 1 and incidents[0].occurrences == 2 and incidents[0].status == 'open'
    alerts = (await db.execute(text("SELECT * FROM alerts WHERE status='active'"))).fetchall()
    assert len(alerts) == 1 and alerts[0].metadata['occurrences'] == 2
    assert (await db.execute(text("SELECT count(*) FROM ncm_notification_outbox"))).scalar() == 1
    jobs = await ncm.backup_jobs(did, 20, 0, db, actor)
    runs = jobs['data'][0]['runs']
    assert runs[0]['failure_category'] == 'authentication' and runs[0]['stage'] == 'connect'
    assert runs[0]['recommended_action'] and 'SECRET' not in str(jobs)

    monkeypatch.setattr(ncm, '_netmiko_fetch', lambda *a, **k: ('cisco_ios', GOOD))
    await ncm_jobs.enqueue(db, did, actor=actor); await db.commit()
    assert (await run_job(db, sessions))['status'] == 'success'
    assert (await db.execute(text('SELECT status FROM ncm_incidents'))).scalar() == 'recovered'
    assert (await db.execute(text("SELECT count(*) FROM alerts WHERE status='active' AND metadata->>'ncm_event'='backup.failed'"))).scalar() == 0
    keys = [r.event_key for r in (await db.execute(text('SELECT event_key FROM ncm_notification_outbox ORDER BY created_at'))).fetchall()]
    assert keys[0].endswith(':open') and keys[-1].endswith(':recovered')


@pytest.mark.asyncio
async def test_transient_failures_retry_and_resume_remaining_types(env, monkeypatch):
    db, sessions, actor = env
    did = await device(db)
    await db.execute(text("UPDATE device_ncm SET config_types='{running,startup}'"))
    await db.commit()
    calls = []

    def flaky(host, platform, user, pw, enable, port, config_type, **k):
        calls.append(config_type)
        if config_type == 'startup' and calls.count('startup') == 1:
            raise TimeoutError('slow')
        return 'cisco_ios', GOOD
    monkeypatch.setattr(ncm, '_netmiko_fetch', flaky)
    await ncm_jobs.enqueue(db, did, actor=actor); await db.commit()
    first = await run_job(db, sessions)
    assert first['status'] == 'queued' and first['failure_category'] == 'timeout'
    await db.execute(text('UPDATE ncm_jobs SET available_at=now()')); await db.commit()
    second = await run_job(db, sessions)
    assert second['status'] == 'success' and calls == ['running', 'startup', 'startup']
    assert (await db.execute(text('SELECT count(*) FROM ncm_incidents'))).scalar() == 0


# --- B10 --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bulk_jobs_report_each_outcome(env):
    db, _, actor = env
    ready = await device(db); busy = await device(db); unprofiled = await device(db)
    await db.execute(text("UPDATE ncm_credentials SET password_enc=NULL WHERE id=(SELECT credential_id FROM device_ncm WHERE device_id=:id)"), {'id': unprofiled})
    plain = uuid4()
    await db.execute(text("INSERT INTO devices(id,hostname,ip_address,device_type,tags) VALUES(:id,'plain','192.0.2.50','router','[]')"), {'id': plain})
    await db.commit()
    await ncm_jobs.enqueue(db, busy, actor=actor); await db.commit()
    result = await ncm.bulk_jobs(ncm.BulkJobs(device_ids=[ready, busy, unprofiled, plain, uuid4()]), db, actor)
    status = {r['device_id']: r['status'] for r in result['results']}
    assert status[str(ready)] == 'queued' and status[str(busy)] == 'already_active'
    assert status[str(unprofiled)] == 'blocked' and status[str(plain)] == 'not_enrolled'
    assert result['not_found'] == 1 and result['queued'] == 1


# --- B16 / B17 ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_version_history_pages_and_flags_protection(env):
    db, _, actor = env
    did = await device(db)
    await db.execute(text('UPDATE device_ncm SET keep_versions=100'))
    await db.commit()
    ids = [await capture(db, did, n, actor=actor, validated=True) for n in range(6)]
    await ncm.approve_baseline(did, ncm.BaselineIn(version_id=ids[0], name='gold', reason='lab review'), db, actor)
    page = await ncm.list_configs(did, 2, 0, 'running', False, db, actor)
    assert page['total'] == 6 and len(page['data']) == 2
    last = await ncm.list_configs(did, 2, 4, 'running', False, db, actor)
    assert last['data'][-1]['id'] == str(ids[0]) and last['data'][-1]['baseline_protected']
    assert last['data'][-1]['active_baseline'] == 'gold'
    protected = await ncm.list_configs(did, 100, 0, 'running', True, db, actor)
    assert [v['id'] for v in protected['data']] == [str(ids[0])]
    drift = (await ncm.baselines(did, db, actor))['data'][0]
    assert drift['drift'] == 'drifted'
    await ncm.retire_baseline(did, UUID(drift['id']), ncm.RetireIn(reason='replaced by change 42'), db, actor)
    with pytest.raises(HTTPException) as error:
        await ncm.pin_config(did, ids[0], ncm.PinIn(pinned=False), db, actor)
    assert error.value.status_code == 409


# --- Scheduler gate ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_scheduler_skips_blocked_devices_with_one_incident(env):
    db, _, actor = env
    did = await device(db)
    await db.execute(text('UPDATE ncm_credentials SET password_enc=NULL'))
    await db.execute(text('UPDATE device_ncm SET schedule_enabled=true'))
    await db.commit()
    result = await ncm._run_scheduled(db)
    assert result['blocked'] == 1 and result['queued'] == 0
    assert (await db.execute(text('SELECT count(*) FROM ncm_jobs'))).scalar() == 0
    row = (await db.execute(text('SELECT last_status,last_failure_category FROM device_ncm'))).first()
    assert row.last_status == 'blocked'
    overview = await ncm.ncm_overview(db, actor)
    assert overview['data'][0]['backup_state'] == 'blocked' and overview['attention']['blocked'] == 1


# --- F01 compliance ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_compliance_evaluates_validated_capture_with_exceptions(env, monkeypatch):
    db, _, actor = env
    did = await device(db)
    monkeypatch.setattr(ncm, '_netmiko_fetch', lambda *a, **k: ('cisco_ios', GOOD))
    await ncm._do_fetch(db, did, actor=actor)
    results = {r['rule_key']: r for r in (await compliance_api.results(None, None, None, did, None, 500, 0, db, actor))['data']}
    assert results['ssh-v2']['status'] == 'pass'
    assert results['snmp-default-community']['status'] == 'fail'
    assert 'public' not in str(results['snmp-default-community']['evidence'])
    rule = UUID(results['snmp-default-community']['rule_id'])
    await compliance_api.create_exception(compliance_api.ExceptionIn(rule_id=rule, device_id=did, owner='netops',
        reason='legacy poller until Q4', expires_at=datetime.now(timezone.utc) + timedelta(days=30)), db, actor)
    after = {r['rule_key']: r for r in (await compliance_api.results(None, None, None, did, None, 500, 0, db, actor))['data']}
    assert after['snmp-default-community']['status'] == 'excepted'
    summary = await compliance_api.summary(30, db, actor)
    assert summary['devices_assessed'] == 1 and summary['trend']


# --- F08 event-triggered backups -----------------------------------------------------------
@pytest.mark.asyncio
async def test_syslog_change_triggers_one_debounced_backup(env):
    db, _, actor = env
    did = await device(db)
    now = datetime.now(timezone.utc)
    for n, message in enumerate(['%SYS-5-CONFIG_I: Configured from console by netops on vty0 (10.0.0.5)',
                                 '%SYS-5-CONFIG_I: Configured from console by netops on vty0 (10.0.0.5)',
                                 '%LINK-3-UPDOWN: Interface Gi0/1, changed state to up']):
        await db.execute(text('INSERT INTO network_events(id,received_at,source_ip,device_id,message) VALUES(:id,:at,:ip,:d,:m)'),
                         {'id': uuid4(), 'at': now - timedelta(minutes=3) + timedelta(seconds=n), 'ip': '192.0.2.1', 'd': did, 'm': message})
    await db.commit()
    assert await ncm_changes.scan_syslog(db) == 2
    assert await ncm_changes.scan_syslog(db) == 0
    result = await ncm_changes.process_pending(db, now=now - timedelta(minutes=2))
    assert result['queued'] == 0                                  # still inside the quiet period
    result = await ncm_changes.process_pending(db)
    assert result['queued'] == 1
    job = (await db.execute(text('SELECT trigger FROM ncm_jobs'))).scalar()
    events = (await ncm.device_change_events(did, db, actor))['data']
    assert job == 'event' and all(e['job_id'] for e in events) and events[0]['reported_user'] == 'netops'


# --- F14 / B20 overview filtering and single-device summary ----------------------------------
@pytest.mark.asyncio
async def test_overview_filters_pages_and_device_summary(env):
    db, _, actor = env
    ids = [await device(db) for _ in range(5)]
    await db.execute(text("UPDATE devices SET vendor='Aruba' WHERE id=:id"), {'id': ids[0]})
    await db.commit()
    page = await ncm.ncm_overview(db, actor, q=None, status=None, vendor='Aruba', device_type=None, location=None,
                                  attention=False, sort='hostname', page=1, page_size=2)
    assert page['filtered_total'] == 1 and page['total_devices'] == 5 and len(page['data']) == 1
    clamped = await ncm.ncm_overview(db, actor, q=None, status=None, vendor=None, device_type=None, location=None,
                                     attention=False, sort='hostname', page=99, page_size=2)
    assert clamped['page'] == 3 and len(clamped['data']) == 1
    summary = await ncm.device_summary(ids[1], db, actor)
    assert summary['device_id'] == str(ids[1]) and 'notify_channel_details' in summary and summary['server_time']


# --- F12 report sections -----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ncm_report_sections_render(env, monkeypatch):
    db, _, actor = env
    did = await device(db)
    monkeypatch.setattr(ncm, '_netmiko_fetch', lambda *a, **k: ('cisco_ios', GOOD))
    await ncm._do_fetch(db, did, actor=actor)
    from app.services.report_sections import build_sections, REPORT_PRESETS
    now = datetime.now(timezone.utc)
    sections = await build_sections(db, REPORT_PRESETS['ncm_protection']['sections'], now - timedelta(days=7), now)
    assert len(sections) == 6
    assert not any('could not be generated' in ' '.join(s['notes']) for s in sections), [s['notes'] for s in sections]
    assert sections[0]['kpis'][0]['value'] == '1 of 1'
