"""Real PostgreSQL enrollment and heartbeat with release build metadata."""
import hashlib
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.api.v1 import sensor_api, sensors
from app.core.database import get_db
from pg_fixture import pg_cluster, pg_db  # noqa: F401


@pytest.mark.asyncio
async def test_release_metadata_survives_enrollment_and_heartbeat(pg_db, monkeypatch, tmp_path):
    token = 'test-only-enrollment-' + uuid4().hex
    sid = uuid4()
    pg_db.execute("INSERT INTO sensors(id,name,enrollment_token_hash) VALUES ($1,$2,$3)",
                  sid, str(sid), hashlib.sha256(token.encode()).hexdigest())
    monkeypatch.setattr(sensors, 'BOOTSTRAP_DIR', tmp_path)
    monkeypatch.setattr(sensor_api, 'SENSOR_ARTIFACT_DIR', tmp_path)

    async def limit(*args, **kwargs):
        pass
    monkeypatch.setattr(sensor_api, '_limit_sensor_request', limit)
    sessions = async_sessionmaker(create_async_engine(pg_db.url, poolclass=NullPool))
    async def database():
        async with sessions() as db:
            yield db
    app = FastAPI()
    app.include_router(sensor_api.router, prefix='/api/v1')
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://sensor.test') as client:
        version = 'sensor-1.24.29-qualification.2+' + 'a' * 64
        first = await client.post('/api/v1/sensor/enroll', json={'enrollment_token': token, 'version': version})
        assert first.status_code == 200
        assert pg_db.scalar('SELECT version FROM sensors WHERE id=$1', sid) == version
        identity = first.json()
        headers = {'X-Sensor-Id': str(sid), 'Authorization': 'Bearer ' + identity['api_key']}
        boundary = 'v' * 128
        heartbeat = await client.post('/api/v1/sensor/heartbeat', headers=headers, json={'version': boundary})
        assert heartbeat.status_code == 200
        assert pg_db.scalar('SELECT version FROM sensors WHERE id=$1', sid) == boundary
        for field, limit_value in [('version', 128), ('hostname', 255), ('os_info', 255)]:
            rejected = await client.post('/api/v1/sensor/heartbeat', headers=headers, json={field: 'x' * (limit_value + 1)})
            assert rejected.status_code == 422
            rejected = await client.post('/api/v1/sensor/enroll', json={'enrollment_token': token, field: 'x' * (limit_value + 1)})
            assert rejected.status_code == 422
        assert pg_db.scalar('SELECT version FROM sensors WHERE id=$1', sid) == boundary


def test_version_migration_preserves_view_identity_grants_and_rows(pg_db):
    from pathlib import Path
    migration = Path(__file__).resolve().parents[2] / 'scripts/migrate-168-sensor-version-width.sql'
    definition = pg_db.scalar("SELECT pg_get_viewdef('service_monitoring_vantages'::regclass, true)")
    pg_db.execute('CREATE OR REPLACE VIEW service_monitoring_vantages AS SELECT NULL::uuid AS service_check_id, NULL::text AS poller_id, NULL::uuid AS sensor_id WHERE false')
    pg_db.execute('ALTER TABLE sensors ALTER COLUMN version TYPE VARCHAR(32), ALTER COLUMN min_supported_version TYPE VARCHAR(32)')
    pg_db.execute('CREATE OR REPLACE VIEW service_monitoring_vantages AS ' + definition)
    pg_db.execute('CREATE VIEW fixture_vantage_dependency AS SELECT * FROM service_monitoring_vantages')
    sid = uuid4()
    version = 'sensor-1.24.29'
    pg_db.execute("INSERT INTO sensors(id,name,version,min_supported_version,status) VALUES ($1,$2,$3,$3,'online')", sid, str(sid), version)
    pg_db.execute("INSERT INTO service_checks(name,check_type,target_host,target_port,default_sensor_id) VALUES ($1,'tcp','127.0.0.1',19090,$2)", str(sid), sid)
    before = pg_db.query('SELECT * FROM fixture_vantage_dependency ORDER BY service_check_id,poller_id')
    view = pg_db.query("SELECT oid,relacl::text,relowner FROM pg_class WHERE oid='service_monitoring_vantages'::regclass")
    pg_db.psql_file(migration)
    assert pg_db.scalar('SELECT version FROM sensors WHERE id=$1', sid) == version
    assert pg_db.query('SELECT * FROM fixture_vantage_dependency ORDER BY service_check_id,poller_id') == before
    assert pg_db.query("SELECT oid,relacl::text,relowner FROM pg_class WHERE oid='service_monitoring_vantages'::regclass") == view
    long_version = version + '+release-build-metadata-longer-than-thirty-two'
    pg_db.execute('UPDATE sensors SET version=$1 WHERE id=$2', long_version, sid)
    pg_db.psql_file(migration)
    assert pg_db.scalar('SELECT version FROM sensors WHERE id=$1', sid) == long_version
    assert pg_db.scalar("SELECT character_maximum_length FROM information_schema.columns WHERE table_name='sensors' AND column_name='version'") == 128
