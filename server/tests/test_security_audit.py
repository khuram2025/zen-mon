from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from pathlib import Path
import ssl
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from app.core import crypto, security
from app.services import external_auth, management_access
from app.api.v1.management_access import AccessPolicy, set_access

@pytest.fixture
def cipher(monkeypatch):
    monkeypatch.setattr(crypto, '_cipher', lambda: AESGCM(bytes(range(32))))

@pytest.mark.parametrize('secret', ['public', ' space preserved ', 'Unicode-秘密', 'x'*1024])
def test_text_encryption_round_trip_and_random_nonce(cipher, secret):
    a, b = crypto.encrypt_text(secret), crypto.encrypt_text(secret)
    assert a != b and secret not in a
    assert crypto.decrypt_secret(a) == secret
    assert crypto.decrypt_secret(secret) == secret

def test_encrypted_config_preserves_nested_values_and_metadata(cipher):
    cfg={'password':'secret', 'url':'https://example.invalid/token', 'headers':{'Authorization':'Bearer secret'}, 'enabled':True}
    stored=crypto.encrypt_config(cfg)
    assert 'secret' not in str(stored)
    assert crypto.decrypt_config(stored)==cfg
    stored['enabled']=False
    assert crypto.decrypt_config(stored)==dict(cfg, enabled=False)

def test_tampered_ciphertext_fails_closed(cipher):
    token=bytearray(crypto.encrypt('secret'));token[-1]^=1
    with pytest.raises(InvalidTag): crypto.decrypt_secret(bytes(token))

@pytest.mark.parametrize('cfg', [{'use_ssl':False,'use_starttls':False}, {}])
def test_ldap_rejects_plaintext_before_connect(cfg):
    with pytest.raises(external_auth.ExternalAuthError): external_auth.ldap_open(cfg,'user','secret')

def test_ldap_starttls_failure_never_binds(monkeypatch):
    import ldap3
    conn=SimpleNamespace(open=Mock(),closed=False,start_tls=Mock(return_value=False),bind=Mock(),unbind=Mock())
    tls=Mock();monkeypatch.setattr(ldap3,'Tls',tls)
    server=Mock();monkeypatch.setattr(ldap3,'Server',server)
    monkeypatch.setattr(ldap3,'Connection',Mock(return_value=conn))
    with pytest.raises(external_auth.ExternalAuthError):
        external_auth.ldap_test_bind({'server':'dc.example.invalid','use_starttls':True})
    assert tls.call_args.kwargs['validate']==ssl.CERT_REQUIRED
    conn.bind.assert_not_called();conn.unbind.assert_called_once()

def test_ldap_verified_tls_before_bind(monkeypatch):
    import ldap3
    events=[]
    conn=SimpleNamespace(open=lambda:events.append('open'),closed=False,start_tls=lambda:events.append('tls') or True,bind=lambda:events.append('bind') or True,unbind=lambda:events.append('close'))
    monkeypatch.setattr(ldap3,'Connection',lambda *a,**k:conn)
    external_auth.ldap_test_bind({'server':'dc.example.invalid','use_starttls':True})
    assert events==['open','tls','bind','close']

def test_policy_default_open_and_invalid_file_fails_closed(tmp_path,monkeypatch):
    path=tmp_path/'policy.json';monkeypatch.setattr(management_access,'POLICY_PATH',path)
    assert management_access.load_policy()=={'web_restricted':False,'ssh_restricted':False,'allowed_cidrs':[]}
    management_access.check_web_access('192.0.2.1')
    path.write_text('{invalid')
    with pytest.raises(HTTPException) as e:management_access.check_web_access('192.0.2.1')
    assert e.value.status_code==503

@pytest.mark.parametrize('ip,expected',[('192.0.2.7',True),('::ffff:192.0.2.7',True),('198.51.100.1',False),('2001:db8::5',True),('invalid',False)])
def test_allowlist_ipv4_ipv6(ip,expected):
    assert management_access.address_allowed(ip,['192.0.2.0/24','2001:db8::/64']) is expected

@pytest.mark.asyncio
async def test_policy_prevents_self_lockout():
    request=SimpleNamespace(client=SimpleNamespace(host='192.0.2.1'))
    with pytest.raises(HTTPException) as e:
        await set_access(AccessPolicy(web_restricted=True,allowed_cidrs=['198.51.100.0/24']),request,db=None,user=None)
    assert e.value.status_code==400

def test_viewer_cannot_read_or_write_management_policy(client,as_viewer):
    assert client.get('/api/v1/system/security/access').status_code==403
    assert client.put('/api/v1/system/security/access',json={'web_restricted':False}).status_code==403

@pytest.mark.asyncio
async def test_session_revocation_checks_database_version():
    user=SimpleNamespace(is_active=True,token_version=2)
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:user)))
    token=security.create_access_token({'sub':'00000000-0000-0000-0000-000000000001','ver':1})
    with pytest.raises(HTTPException) as e:
        await security.get_current_user(credentials=HTTPAuthorizationCredentials(scheme='Bearer',credentials=token),db=db)
    assert e.value.status_code==401
    token=security.create_access_token({'sub':'00000000-0000-0000-0000-000000000001','ver':2})
    assert await security.get_current_user(credentials=HTTPAuthorizationCredentials(scheme='Bearer',credentials=token),db=db) is user

@pytest.mark.asyncio
async def test_missing_token_is_401():
    with pytest.raises(HTTPException) as e:await security.get_current_user(credentials=None,db=None)
    assert e.value.status_code==401

def test_long_passwords_do_not_truncate():
    a='a'*72+'one'; b='a'*72+'two'; hashed=security.hash_password(a)
    assert security.verify_password(a,hashed)
    assert not security.verify_password(b,hashed)

@pytest.mark.asyncio
async def test_snmp_secrets_not_in_process_arguments(monkeypatch):
    from app.api.v1 import snmp
    captured={}
    async def spawn(*args,**kwargs):
        assert '-c' not in args and 'private-secret' not in args
        folder=Path(kwargs['env']['SNMPCONFPATH']);file=folder/'snmp.conf'
        assert file.stat().st_mode & 0o777 == 0o600
        assert folder.stat().st_mode & 0o777 == 0o700
        assert 'private-secret' in file.read_text()
        captured['folder']=folder
        return SimpleNamespace(communicate=AsyncMock(return_value=(b'OK',b'')),returncode=0)
    monkeypatch.setattr(snmp.asyncio,'create_subprocess_exec',spawn)
    _,out,_=await snmp._private_snmpget(['snmpget','-c','private-secret','127.0.0.1','1.3.6'],1)
    assert out==b'OK' and not captured['folder'].exists()

@pytest.mark.asyncio
async def test_snmp_configuration_injection_rejected():
    from app.api.v1.snmp import _private_snmpget
    with pytest.raises(ValueError): await _private_snmpget(['snmpget','-c','value\ndefCommunity other'],1)


def test_notification_transport_refuses_http_and_tls_bypass():
    from app.services.notification_transport import NotificationHTTPClient
    client=NotificationHTTPClient(verify=False)
    with pytest.raises(ValueError):client.build_request('POST','http://example.invalid/secret')
    assert str(client.build_request('POST','https://example.invalid/secret').url).startswith('https://')

@pytest.mark.asyncio
async def test_login_quota_returns_429_before_password_check(monkeypatch):
    from app.api.v1 import auth
    from app.schemas.auth import LoginRequest
    from app.services import sensor_rate_limit
    monkeypatch.setattr(sensor_rate_limit,'enforce_sensor_quota',AsyncMock(side_effect=HTTPException(429,'quota')))
    login_impl=AsyncMock();monkeypatch.setattr(auth,'_login_impl',login_impl)
    with pytest.raises(HTTPException) as e:
        await auth.login(LoginRequest(username='test',password='bad'),db=None,request=SimpleNamespace(client=SimpleNamespace(host='192.0.2.1')))
    assert e.value.status_code==429 and e.value.headers['Retry-After']=='60'
    login_impl.assert_not_called()


@pytest.mark.parametrize('authenticated', [True, False])
def test_radius_requires_authenticated_reply(monkeypatch, authenticated):
    from pyrad import packet
    from pyrad.client import Client
    def send(client, req):
        req.RequestPacket()
        reply=req.CreateReply()
        if authenticated: reply.add_message_authenticator()
        return packet.AuthPacket(packet=reply.ReplyPacket(), secret=req.secret, dict=req.dict)
    monkeypatch.setattr(Client,'SendPacket',send)
    cfg={'server':'127.0.0.1','secret':'test-only-radius-secret'}
    if authenticated:assert external_auth.radius_authenticate(cfg,'test-user','test-password') is not None
    else:
        with pytest.raises(external_auth.ExternalAuthError):external_auth.radius_authenticate(cfg,'test-user','test-password')


def test_sensor_seed_hashes_console_password_and_hardens_ssh():
    from app.api.v1.sensors import _console_user_cloud_init, _bootstrap_cloud_init
    data=SimpleNamespace(enable_console_user=True,console_username='testadmin',console_password='test-only-strong-password')
    user_data=_console_user_cloud_init(data)
    assert data.console_password not in user_data and 'plain_text_passwd' not in user_data
    assert '$6$rounds=200000$' in user_data
    cloud=_bootstrap_cloud_init('https://controller.example.invalid','test-token','test-probe',create_data=data)
    assert 'PermitRootLogin no' in cloud and 'X11Forwarding no' in cloud
