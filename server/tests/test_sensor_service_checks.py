from uuid import uuid4
import pytest
from app.services.sensor_service_checks import service_auth_config, supports_service_auth
from app.schemas.sensor import ConfigServiceCheck

@pytest.mark.parametrize('version,expected', [(None,False),('1.23.4',False),('1.23.5',True),('1.24.0',True),('2.0.0',True),('v1.23.5',True),('invalid',False),('sensor-1.23.5',True),('sensor-1.23.4',False)])
def test_auth_capability(version, expected):
    assert supports_service_auth(version) == expected

def test_decryption_failure_is_closed_and_does_not_expose_secret(monkeypatch):
    def fail(value): raise ValueError('sensitive material')
    monkeypatch.setattr('app.services.sensor_service_checks.decrypt', fail)
    result = service_auth_config({'credential_id': uuid4(), 'credential_auth_type': 'ntlm', 'secret_cipher': b'bad'})
    assert result['credential_secret'] == ''
    assert result['credential_error'] == 'Service credential could not be decrypted'
    assert 'sensitive' not in str(result)

def test_saved_auth_and_nullable_workflow_match_controller_model(monkeypatch):
    monkeypatch.setattr('app.services.sensor_service_checks.decrypt', lambda value: 'test-password')
    result = service_auth_config({'credential_id': uuid4(), 'credential_auth_type': 'ntlm', 'credential_username': 'test-user', 'secret_cipher': b'encrypted', 'workflow_steps': [{'name':'Health', 'url':'https://example.test', 'body':None, 'content_match':None}]})
    config = ConfigServiceCheck(id=str(uuid4()), name='Test', check_type='http', **result)
    assert config.credential_secret == 'test-password'
    assert config.workflow_steps[0].content_match is None
    assert 'test-password' not in repr(config)

def test_missing_credential_fails_closed():
    assert service_auth_config({'credential_id': uuid4()})['credential_error']

def test_sensor_config_rejects_plain_http_before_reading_secrets():
    import asyncio
    from fastapi import HTTPException, Response
    from starlette.requests import Request
    from app.api.v1.sensor_api import get_config
    request = Request({'type':'http','scheme':'http','server':('test',80),'path':'/sensor/config','headers':[],'query_string':b''})
    with pytest.raises(HTTPException) as error:
        asyncio.run(get_config(request, Response(), db=None))
    assert error.value.status_code == 400
    assert 'HTTPS' in error.value.detail
