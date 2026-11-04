"""NCCM capture deadline, progress and key-failure reporting on the real schema.

Runs migration 183 and the collection entry point (_do_fetch) against the
disposable PostgreSQL fixture; the device connection is stubbed, nothing
reaches an installed database or a device.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures)

ROOT_SCRIPTS = __import__('pathlib').Path(__file__).resolve().parents[2] / 'scripts'


def test_migration_183_columns_bounds_and_replay(pg_db):
    cols = {(t, c): d for t, c, d in pg_db.query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE column_name IN "
        "('capture_timeout_seconds','progress','capture_stats')")}
    assert cols[('ncm_credentials', 'capture_timeout_seconds')] == 'integer'
    assert cols[('device_ncm', 'capture_timeout_seconds')] == 'integer'
    assert cols[('ncm_jobs', 'progress')] == 'jsonb' and cols[('ncm_backup_runs', 'capture_stats')] == 'jsonb'
    pg_db.execute("INSERT INTO ncm_credentials (name, username) VALUES ('inherit', 'u')")
    assert pg_db.scalar("SELECT capture_timeout_seconds FROM ncm_credentials WHERE name='inherit'") is None
    pg_db.execute("INSERT INTO ncm_credentials (name, username, capture_timeout_seconds) VALUES ('big', 'u', 3600)")
    for bad in (59, 3601):
        with pytest.raises(Exception, match='capture_timeout_range'):
            pg_db.execute(f"INSERT INTO ncm_credentials (name, username, capture_timeout_seconds) VALUES ('x', 'u', {bad})")
    # Forward-only and replay-safe: applying it again changes nothing.
    pg_db.psql_file(ROOT_SCRIPTS / 'migrate-183-ncm-capture-deadline-progress.sql')
    assert pg_db.scalar("SELECT capture_timeout_seconds FROM ncm_credentials WHERE name='big'") == 3600


def _seed(db, *, auth='password', key=None, device_timeout=None, profile_timeout=None, platform='fortinet'):
    device, cred = uuid.uuid4(), uuid.uuid4()
    db.execute("INSERT INTO devices (id, hostname, ip_address, device_type, vendor) VALUES ($1, 'fgt-201f', '192.0.2.10', 'firewall', 'Fortinet')", device)
    db.execute("INSERT INTO ncm_credentials (id, name, username, password_enc, auth_method, private_key_enc, capture_timeout_seconds)"
               " VALUES ($1, 'p', 'backup', $2, $3, $4, $5)", cred, b'pw', auth, key, profile_timeout)
    db.execute("INSERT INTO device_ncm (device_id, credential_id, platform, enabled, capture_timeout_seconds)"
               " VALUES ($1, $2, $3, true, $4)", device, cred, platform, device_timeout)
    job = db.scalar("INSERT INTO ncm_jobs (device_id, config_types, trigger, actor_name, status, attempts)"
                    " VALUES ($1, ARRAY['running'], 'manual', 'test', 'running', 1) RETURNING id", device)
    return device, job


async def _run(url, monkeypatch, device, job, fetch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.api.v1 import ncm
    monkeypatch.setattr(ncm.crypto, 'decrypt', lambda value: bytes(value).decode())
    monkeypatch.setattr(ncm, '_netmiko_fetch', fetch)
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            with pytest.raises(ncm._FetchError) as exc:
                await ncm._do_fetch(db, device, config_type='running', job_id=job, progress_sessions=sessions)
        return exc.value
    finally:
        await engine.dispose()


def test_encrypted_key_without_passphrase_is_reported_as_key_problem(pg_db, monkeypatch):
    text = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(b'secret-pass')).decode()
    device, job = _seed(pg_db, auth='key', key=text.encode())

    def never(*a, **k):
        pytest.fail('no connection may be attempted with an unloadable key')
    error = asyncio.run(_run(pg_db.url, monkeypatch, device, job, never))
    assert error.category == 'key' and not error.retryable and 'passphrase' in str(error)
    stage, category, message = pg_db.query("SELECT stage, failure_category, error_code FROM ncm_backup_runs WHERE device_id=$1", device)[0]
    assert (stage, category) == ('key', 'key') and 'encrypted' in message
    assert pg_db.scalar("SELECT last_failure_category FROM device_ncm WHERE device_id=$1", device) == 'key'
    from app.api.v1.ncm import _run_view
    from app.services.ncm_errors import GUIDANCE
    row = {'id': uuid.uuid4(), 'config_type': 'running', 'trigger': 'manual', 'status': 'failed', 'started_at': None,
           'stage': stage, 'failure_category': category, 'error_code': message}
    view = _run_view(row)
    assert view['stage_label'] == 'SSH key loading' and view['recommended_action'] == GUIDANCE['key']


def test_deadline_override_progress_and_stats_are_persisted(pg_db, monkeypatch):
    device, job = _seed(pg_db, device_timeout=1800, profile_timeout=600)
    seen = {}

    def slow_fetch(ip, platform, username, password, enable, port, config_type, *, pkey, legacy_compat,
                   observed_key, capture_timeout, progress):
        import time as real_time
        from app.services.ncm_collection import CaptureError
        seen.update(capture_timeout=capture_timeout, pkey=pkey, password=password)
        progress.update(phase='capturing', command='show full-configuration', platform='fortinet',
                        bytes=1_200_000, lines=38_000, pages=650, deadline_seconds=capture_timeout)
        real_time.sleep(3.5)                     # let the 3 s progress writer persist a row
        raise CaptureError('Incomplete capture: collection deadline exceeded (1800 s, 1.1 MiB and 650 pages received)')
    error = asyncio.run(_run(pg_db.url, monkeypatch, device, job, slow_fetch))
    assert seen == {'capture_timeout': 1800, 'pkey': None, 'password': 'pw'}       # device beats profile
    assert error.category == 'incomplete' and 'deadline exceeded' in str(error)
    progress = pg_db.scalar("SELECT progress FROM ncm_jobs WHERE id=$1", job)
    progress = json.loads(progress) if isinstance(progress, str) else progress
    assert progress['bytes'] == 1_200_000 and progress['pages'] == 650 and progress['config_type'] == 'running'
    assert progress['deadline_seconds'] == 1800 and progress['phase'] == 'finished'
    stats = pg_db.scalar("SELECT capture_stats FROM ncm_backup_runs WHERE device_id=$1", device)
    stats = json.loads(stats) if isinstance(stats, str) else stats
    assert stats['lines'] == 38_000 and stats['command'] == 'show full-configuration'


def test_device_view_reports_effective_deadline_and_source(pg_db):
    from types import SimpleNamespace
    from app.api.v1.ncm import _deadline_view
    view = _deadline_view(SimpleNamespace(platform='fortinet', device_capture_timeout=None, profile_capture_timeout=None))
    assert view == {'capture_timeout_seconds': None, 'profile_capture_timeout_seconds': None,
                    'capture_timeout_effective': 900, 'capture_timeout_source': 'driver'}
    view = _deadline_view(SimpleNamespace(platform='cisco_ios', device_capture_timeout=None, profile_capture_timeout=600))
    assert (view['capture_timeout_effective'], view['capture_timeout_source']) == (600, 'profile')
