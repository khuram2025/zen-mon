import asyncio
import hashlib
import pytest
from fastapi import HTTPException
from app.api.v1.sensor_api import _authenticate
from app.services.sensor_snmp import sensor_snmp_config


def test_linked_snmp_credentials_and_owned_device_scope():
    class Result:
        def mappings(self): return self
        def all(self): return [{'id': 'probe-target', 'snmp_version': '2c', 'snmp_community': 'old',
                               'snmp_retries': 0, 'credential': {'snmp_version': '3', 'v3_username': 'probe',
                               'v3_auth_protocol': 'SHA256', 'v3_auth_passphrase': 'example', 'port': 1161}}]
    class DB:
        async def execute(self, sql, params):
            assert "JOIN device_monitoring_vantages owner" in str(sql)
            assert 'owner.sensor_id = :sid' in str(sql)
            assert params == {'sid': 'sensor-id'}
            return Result()
    config = asyncio.run(sensor_snmp_config('sensor-id', DB()))['probe-target']
    assert config.version == '3'
    assert config.v3_auth_passphrase == 'example'
    assert config.port == 1161
    assert config.retries == 0


def test_enrolled_probe_cannot_fetch_secrets_before_authorization():
    class Result:
        def mappings(self): return self
        def first(self): return {'status': 'pending', 'api_key_hash': hashlib.sha256(b'key').hexdigest(),
                                 'bootstrap_config': {'authorization_pending': True}}
    class DB:
        async def execute(self, sql, params): return Result()
    sid = '00000000-0000-0000-0000-000000000001'
    with pytest.raises(HTTPException) as failure:
        asyncio.run(_authenticate(sid, 'key', DB()))
    assert failure.value.status_code == 403
    assert asyncio.run(_authenticate(sid, 'key', DB(), allow_pending=True))['status'] == 'pending'
