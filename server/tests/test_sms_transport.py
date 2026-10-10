"""SMS request acceptance uses an isolated HTTP transport, never a provider."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1 import settings, alert_engine
from app.services import ncm_delivery, sms_transport
from app.services.alert_delivery_log import DeliveryRejected


@pytest.fixture
def http_fixture(monkeypatch):
    requests = []
    state = {'status': 200, 'body': {'Success': True}}
    def handler(request):
        requests.append(request)
        return httpx.Response(state['status'], json=state['body'])
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    for module in (settings, alert_engine, ncm_delivery, sms_transport):
        monkeypatch.setattr(module, 'NotificationHTTPClient', factory)
    return requests, state


def gateway(**overrides):
    return {'provider': 'custom_http', 'api_url': 'https://sms.fixture.invalid/send',
            'http_method': 'POST', 'content_type': 'application/x-www-form-urlencoded',
            'request_template': 'AppSid=fixture-key&Recipient={recipients}&Body={message}&Sender={sender}',
            'sender_name': 'Ops & Support', **overrides}


def channel_db(config, *, linked=None, available=True, enabled=True):
    link = linked or uuid4()
    channel = NS(id=uuid4(), name='Fixture channel', type='sms', config={'gateway_id': str(link), 'phone_numbers': '+15550000001'}, gateway_id=link, enabled=True)
    gw = NS(config=config, enabled=enabled)
    db = NS(execute=AsyncMock(side_effect=[NS(first=lambda: channel), NS(first=lambda: gw if available else None)]))
    return channel, db


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['gateway', 'channel', 'alert', 'ncm'])
async def test_all_sms_paths_encode_form_and_set_content_type(path, http_fixture, monkeypatch):
    requests, _ = http_fixture
    cfg = gateway()
    message = 'CPU > 90% & rising + "quotes"\nجديد'
    if path == 'gateway':
        await settings._do_sms_test(cfg, '+15550000001')
    elif path == 'channel':
        ch, db = channel_db(cfg)
        await settings.test_channel(ch.id, db, NS())
        assert db.execute.call_args_list[1].args[1]['id'] == ch.gateway_id
        assert db.execute.call_args_list[1].args[1]['type'] == 'sms'
    elif path == 'alert':
        assert await alert_engine._send_sms(cfg, '+15550000001', message)
    else:
        await ncm_delivery.sms_send(cfg, '+15550000001', {'subject': message})
    assert len(requests) == 1
    request = requests[0]
    assert request.headers['content-type'] == 'application/x-www-form-urlencoded'
    body = parse_qs(request.content.decode(), strict_parsing=True)
    assert body['AppSid'] == ['fixture-key']
    assert body['Recipient'] == ['+15550000001']
    assert body['Sender'] == ['Ops & Support']
    assert set(body) == {'AppSid', 'Recipient', 'Body', 'Sender'}
    if path in ('alert', 'ncm'):
        assert body['Body'] == [message]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(402, {}), (500, {}), (302, {}), (200, {'Success': False}), (200, {'success': 'false'})])
async def test_channel_rejection_is_failure_not_success(status, body, http_fixture):
    requests, state = http_fixture
    state.update(status=status, body=body)
    ch, db = channel_db(gateway())
    with pytest.raises(HTTPException) as failure:
        await settings.test_channel(ch.id, db, NS())
    assert failure.value.status_code == 502
    assert 'provider' in failure.value.detail
    assert 'fixture-key' not in failure.value.detail
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_alert_and_ncm_reject_provider_200_failure(http_fixture):
    _, state = http_fixture
    state['body'] = {'Success': False}
    for action in [lambda: alert_engine._send_sms(gateway(), '15550000001', 'test'),
                   lambda: ncm_delivery.sms_send(gateway(), '15550000001', {'subject': 'test'})]:
        with pytest.raises(DeliveryRejected):
            await action()


@pytest.mark.asyncio
async def test_json_strings_and_bearer_auth(http_fixture):
    requests, _ = http_fixture
    cfg = gateway(content_type='application/json', auth_type='bearer', auth_token_value='fixture-token',
                  request_template='{"to":"{recipients}","body":"{message}"}')
    message = '"quoted" & slash\\\nجديد'
    await sms_transport.send_sms_request(cfg, {'recipients': '+15550000001', 'message': message})
    import json
    assert json.loads(requests[0].content) == {'to': '+15550000001', 'body': message}
    assert requests[0].headers['Authorization'] == 'Bearer fixture-token'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bad_json_never_sends_or_retries(http_fixture):
    requests, _ = http_fixture
    with pytest.raises(HTTPException) as failure:
        await settings._do_sms_test(gateway(content_type='application/json', request_template='invalid secret-fixture-json'), '15550000001')
    assert failure.value.status_code == 400
    assert 'secret-fixture-json' not in failure.value.detail
    assert not requests


@pytest.mark.asyncio
async def test_get_message_is_encoded_and_custom_auth_preserved(http_fixture):
    requests, _ = http_fixture
    cfg = gateway(http_method='GET', content_type='', auth_type='basic', auth_username='fixture', auth_password='fixture-password')
    await sms_transport.send_sms_request(cfg, {'recipients': '15550000001', 'message': 'CPU & rising + 90%', 'sender': 'Ops'})
    assert requests[0].url.params['Body'] == 'CPU & rising + 90%'
    assert requests[0].headers['authorization'].startswith('Basic ')


@pytest.mark.asyncio
async def test_custom_content_type_fallback_and_case_normalization(http_fixture):
    requests, _ = http_fixture
    cfg = gateway(content_type='', custom_headers={'content-type': 'application/x-www-form-urlencoded; charset=utf-8'})
    await sms_transport.send_sms_request(cfg, {'recipients': '+15550000001', 'message': 'a&b', 'sender': 'Ops'})
    assert parse_qs(requests[0].content.decode())['Body'] == ['a&b']
    cfg = gateway(custom_headers={'content-type': 'text/plain'})
    await sms_transport.send_sms_request(cfg, {'recipients': '15550000001', 'message': 'test', 'sender': 'Ops'})
    assert requests[-1].headers['content-type'] == 'application/x-www-form-urlencoded'


@pytest.mark.asyncio
@pytest.mark.parametrize('available,enabled', [(False, True), (True, False)])
async def test_invalid_explicit_gateway_never_falls_back_or_sends(available, enabled, http_fixture):
    requests, _ = http_fixture
    ch, db = channel_db(gateway(), available=available, enabled=enabled)
    with pytest.raises(HTTPException):
        await settings.test_channel(ch.id, db, NS())
    assert len(db.execute.call_args_list) == 2
    assert not requests


def test_serialized_channel_uses_authoritative_saved_gateway():
    linked = uuid4()
    row = NS(id=uuid4(), name='Fixture', type='sms', enabled=True, config={'gateway_id': str(uuid4()), 'phone_numbers': '15550000001'}, gateway_id=linked, gateway_name='Selected fixture', created_at=None, updated_at=None)
    result = settings._row_to_channel(row)
    assert result['gateway_id'] == result['config']['gateway_id'] == str(linked)
    assert result['gateway_name'] == 'Selected fixture'


@pytest.mark.asyncio
async def test_connection_failure_is_sanitized_and_not_retried(monkeypatch):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ConnectError('https://sms.fixture.invalid/?token=private-fixture-value', request=request)
    monkeypatch.setattr(settings, 'NotificationHTTPClient', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(HTTPException) as failure:
        await settings._do_sms_test(gateway(), '15550000001')
    assert failure.value.detail == 'SMS gateway connection failed'
    assert count == 1


def test_placeholder_values_are_not_recursively_substituted():
    assert sms_transport.fill_template('{message}', {'message': 'literal {sender}', 'sender': 'Ops'}, is_get=False) == 'literal {sender}'


@pytest.mark.asyncio
async def test_gateway_selection_round_trip_real_postgres(http_fixture, monkeypatch):
    """Opt-in disposable PostgreSQL only; includes encrypted config and typed lookup."""
    import os
    url = os.getenv('SMS_TEST_PG_URL')
    if not url:
        pytest.skip('SMS_TEST_PG_URL must point to a disposable database')
    import json
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from app.core import crypto
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    monkeypatch.setattr(crypto, '_cipher', lambda: AESGCM(bytes(range(32))))
    monkeypatch.setattr(settings, 'write_audit_log', AsyncMock())
    schema = 'sms_fixture_' + uuid4().hex
    setup = create_async_engine(url)
    tested = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    try:
        async with setup.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {schema}'))
            await conn.execute(text(f'''CREATE TABLE {schema}.notification_gateways (
                id uuid PRIMARY KEY, name text, type text, config jsonb, enabled boolean, is_default boolean)'''))
            await conn.execute(text(f'''CREATE TABLE {schema}.notification_channels (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text, type text, config jsonb, enabled boolean,
                gateway_id uuid REFERENCES {schema}.notification_gateways(id), created_at timestamptz, updated_at timestamptz)'''))
        selected, default = uuid4(), uuid4()
        async with AsyncSession(tested) as db:
            for gateway_id, is_default in [(selected, False), (default, True)]:
                cfg = gateway(api_url='https://' + ('selected' if not is_default else 'default') + '.fixture.invalid/send')
                await db.execute(text('INSERT INTO notification_gateways VALUES(:id,:name,\'sms\',CAST(:cfg AS jsonb),true,:is_default)'),
                    {'id': gateway_id, 'name': 'Selected fixture' if not is_default else 'Default fixture',
                     'cfg': json.dumps(crypto.encrypt_config(cfg)), 'is_default': is_default})
            await db.commit()
            created = await settings.create_channel(settings.ChannelCreate(name='Fixture SMS', type='sms',
                config={'gateway_id': str(selected), 'phone_numbers': '+15550000001'}), db, NS())
            assert created['gateway_id'] == str(selected)
            raw = (await db.execute(text('SELECT config FROM notification_channels'))).scalar()
            assert set(raw) == {'_encrypted_v1'}
            listed = await settings.list_channels(db, NS())
            assert listed['data'][0]['gateway_name'] == 'Selected fixture'
            renamed = await settings.update_channel(created['id'], settings.ChannelUpdate(name='Renamed fixture'), db, NS())
            assert renamed['gateway_id'] == str(selected)
            accepted = await settings.test_channel(created['id'], db, NS())
            assert accepted['status_code'] == 200
            assert http_fixture[0][-1].url.host == 'selected.fixture.invalid'
            updated = await settings.update_channel(created['id'], settings.ChannelUpdate(config={
                'phone_numbers': '+15550000002', 'gateway_id': str(selected)}), db, NS())
            assert updated['gateway_id'] == updated['config']['gateway_id'] == str(selected)
            unlinked = await settings.update_channel(created['id'], settings.ChannelUpdate(config={
                'phone_numbers': '+15550000002'}), db, NS())
            assert unlinked['gateway_id'] is None
            await settings.test_channel(created['id'], db, NS())
            assert http_fixture[0][-1].url.host == 'default.fixture.invalid'
    finally:
        await tested.dispose()
        async with setup.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        await setup.dispose()
