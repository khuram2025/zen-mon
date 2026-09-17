from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import hashlib
from unittest.mock import AsyncMock
import pytest
from app.services import ncm_content as content, ncm_collection as collection
from app.services.ncm_status import backup_status
from app.api.v1.ncm import NcmEnroll, ConfigCapture


@pytest.mark.parametrize('before,after', [
    ('enable secret 9 first', 'enable secret 9 second'),
    ('snmp-server community old RO','snmp-server community new RO'),
    ('set password ENC old','set password ENC new'),
    ('-----BEGIN PRIVATE KEY-----\naaaa\n-----END PRIVATE KEY-----',
     '-----BEGIN PRIVATE KEY-----\nbbbb\n-----END PRIVATE KEY-----'),
])
def test_sensitive_changes_detected_without_disclosure(before, after):
    result=content.config_diff(before,after)
    assert not result['identical'] and result['sensitive_changed']
    assert result['added']==1 or result['added']==3
    assert after not in result['diff'] and before not in result['diff']
    assert 'Sensitive configuration changed' in result['diff']
    assert content.config_diff(before,after,raw=True)['diff'] != result['diff']


def test_noise_does_not_hide_real_changes():
    a='! Last configuration change yesterday\nntp clock-period 123\nhostname edge\nend\n'
    b='! Last configuration change today\nntp clock-period 456\nhostname edge\nend\n'
    assert content.config_diff(a,b)['identical']
    assert not content.config_diff(a,b,normalized=False)['identical']
    assert not content.config_diff(a,b.replace('edge','core'))['identical']


@pytest.mark.parametrize('raw,hidden',[
    ('crypto pki certificate chain TP\n certificate ca 01\n  DEADBEEF\n  quit\nhostname edge','DEADBEEF'),
    ('<config>\n<private-key>\nvalue\n</private-key>\n</config>','value'),
    ('-----BEGIN PRIVATE KEY-----\nincomplete','incomplete'),
    ('local-user admin password irreversible-cipher encrypted','encrypted'),
])
def test_structured_secret_redaction(raw,hidden):
    assert hidden not in content.redact_config(raw)


@pytest.mark.parametrize('platform,raw',[
    ('paloalto_panos','<config><devices>'),
    ('fortinet','config system global\n set hostname edge\n'),
    ('huawei','sysname edge\n#\n'),
])
def test_structured_truncation_rejected(platform,raw):
    with pytest.raises(collection.CaptureError): collection.validate_config(raw,platform)


@pytest.mark.parametrize('value',['',' \n','a\x00b'])
def test_invalid_import(value):
    with pytest.raises(ValueError): ConfigCapture(content=value)


@pytest.mark.parametrize('platform,value',[
    ('linux','PRETTY_NAME=Ubuntu'),('cisco_ios','hostname edge\ninterface x\n'),
    ('cisco_ios','% Invalid input detected'),('cisco_ios','hostname edge\n--More--\nend'),
    ('huawei','#\n#'),('fortinet','Command fail. Return code -61'),
])
def test_reject_invalid_device_output(platform,value):
    with pytest.raises(ValueError): collection.validate_config(value,platform)


@pytest.mark.parametrize('params',[
    {'platform':'linux'}, {'schedule_enabled':True,'schedule_type':'daily','schedule_time':'24:00'},
    {'schedule_enabled':True,'schedule_type':'weekly','schedule_time':'12:00','schedule_days':[]},
    {'schedule_days':[1,1]}, {'config_types':['running','running']}, {'freshness_hours':0},
])
def test_invalid_enrollment(params):
    with pytest.raises(ValueError): NcmEnroll(**params)


class Clock:
    value=0
    def monotonic(self): return self.value
    def sleep(self,n): self.value+=n


class Session:
    def __init__(self,chunks): self.chunks=iter(chunks); self.sent=[]
    def clear_buffer(self): pass
    def write_channel(self,value): self.sent.append(value)
    def read_channel(self): return next(self.chunks,'')


def test_delayed_paged_capture_and_prompt_in_banner(monkeypatch):
    clock=Clock()
    monkeypatch.setattr(collection,'time',clock)
    conn=Session(['show running-config\r\nhostname edge\r\nbanner motd ^edge#^\r\n--More--',
                  '', 'interface x\r\nend\r\nedge#'])
    result=collection.run_command(conn,'show running-config','edge#')
    assert 'interface x' in result and 'banner motd' in result
    assert result.endswith('end\n') and 'show running-config' not in result
    assert conn.sent.count(' ')==1


def test_capture_without_final_prompt_fails(monkeypatch):
    monkeypatch.setattr(collection,'time',Clock())
    with pytest.raises(collection.CaptureError,match='Incomplete'):
        collection.run_command(Session(['hostname edge\nend\n']), 'show running-config','edge#',idle_timeout=2)


def test_fortios_connection_never_changes_console(monkeypatch):
    from netmiko.fortinet.fortinet_ssh import FortinetSSH
    commands=[]
    def init(self,**kwargs):
        self.session_preparation()
    monkeypatch.setattr(FortinetSSH,'__init__',init)
    monkeypatch.setattr(FortinetSSH,'_test_channel_read',lambda self,**k:'edge#')
    monkeypatch.setattr(FortinetSSH,'set_base_prompt',lambda self:None)
    monkeypatch.setattr(FortinetSSH,'find_prompt',lambda self:'edge#')
    monkeypatch.setattr(FortinetSSH,'write_channel',lambda self,v:commands.append(v))
    monkeypatch.setattr(FortinetSSH,'disconnect',lambda self:self.cleanup())
    monkeypatch.setattr(FortinetSSH,'send_multiline',lambda *a,**k:pytest.fail('Persistent commands attempted'))
    monkeypatch.setattr(collection,'run_command',lambda *a,**k:'config system global\n set hostname edge\nend\n')
    platform,result=collection.fetch_config('192.0.2.1','fortinet','test','test','',22)
    assert platform=='fortinet' and commands==['exit\r']


def state(**overrides):
    now=datetime.now(timezone.utc)
    data=dict(ip='192.0.2.1',device_type='router',eligible_override=None,
              config_types=['running'],verified_types={'running':now.isoformat()},
              ncm_enabled=True,platform='cisco_ios',last_status='success',
              freshness_hours=24,validated_at=now,versions=1)
    data.update(overrides)
    return backup_status(NS(**data),now)


def test_coverage_requires_all_types_and_validated_runs():
    assert state()['fresh']
    assert not state(config_types=['running','startup'])['fresh']
    assert not state(verified_types={})['fresh']
    assert not state(ncm_enabled=False)['fresh']
    assert state(ip='127.0.0.1')['backup_state']=='excluded'
    assert state(ip=None)['backup_state']=='excluded'
    assert state(device_type='server')['backup_state']=='excluded'
    assert not state(verified_types={'running':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()})['fresh']


def test_encrypted_integrity_and_tamper_detection(monkeypatch):
    from app.core import crypto
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    monkeypatch.setattr(crypto,'_cipher',lambda:AESGCM(bytes(range(32))))
    raw='hostname test\nenable secret 9 private\nend\n'
    token=content.encrypt_content(raw)
    row=NS(content_enc=token,content='',content_hash=hashlib.sha256(raw.encode()).hexdigest())
    assert raw.encode() not in token and content.read_content(row)==raw
    row.content_hash='bad'
    with pytest.raises(ValueError,match='integrity'): content.read_content(row)
    row.content_enc=token[:-1]+bytes([token[-1]^1])
    with pytest.raises(Exception): content.read_content(row)


@pytest.mark.asyncio
async def test_delivery_rejects_http_failure(monkeypatch):
    from app.services import ncm_delivery
    import httpx
    ch=NS(type='webhook',enabled=True,config={'url':'https://sink.invalid'},gateway_id=None)
    db=NS(execute=AsyncMock(return_value=NS(first=lambda:ch)))
    response=httpx.Response(503,request=httpx.Request('POST','https://sink.invalid'))
    client=AsyncMock()
    client.__aenter__.return_value=client
    client.post.return_value=response
    monkeypatch.setattr(ncm_delivery,'NotificationHTTPClient',lambda **kw:client)
    with pytest.raises(httpx.HTTPStatusError):
        await ncm_delivery.send(db,['test'],{'event_id':'event','hostname':'lab','subject':'test','body':'test'})
    response.status_code=204
    assert await ncm_delivery.send(db,['test'],{'event_id':'event'})==1


@pytest.fixture
def ssh_cli(tmp_path,monkeypatch):
    """Actual SSH transport on loopback; no appliance/device connection."""
    import paramiko
    import socket
    import threading
    import time
    key=paramiko.ECDSAKey.generate()
    listener=socket.socket()
    listener.bind(('127.0.0.1',0)); listener.listen(1); listener.settimeout(.2)
    port=listener.getsockname()[1]
    path=tmp_path/'known_hosts'
    keys=paramiko.HostKeys(); keys.add(f'[127.0.0.1]:{port}',key.get_name(),key); keys.save(str(path))
    monkeypatch.setattr(collection,'KNOWN_HOSTS',str(path))
    stopped=threading.Event()
    commands=[]
    class Server(paramiko.ServerInterface):
        def check_auth_password(self,username,password):
            return paramiko.AUTH_SUCCESSFUL if (username,password)==('fixture','fixture') else paramiko.AUTH_FAILED
        def get_allowed_auths(self,username): return 'password'
        def check_channel_request(self,kind,chanid): return paramiko.OPEN_SUCCEEDED
        def check_channel_pty_request(self,*args): return True
        def check_channel_shell_request(self,*args): return True
    def serve():
        transport=None
        try:
            while not stopped.is_set():
                try: sock,_=listener.accept(); break
                except socket.timeout: continue
            else: return
            transport=paramiko.Transport(sock); transport.add_server_key(key)
            transport.start_server(server=Server())
            channel=transport.accept(10)
            if channel is None:return
            channel.settimeout(.2)
            time.sleep(.1); channel.send(b'edge#')
            pending=''
            while not stopped.is_set():
                try: data=channel.recv(65536)
                except socket.timeout: continue
                if not data:break
                pending+=data.decode().replace('\r','\n')
                while '\n' in pending:
                    command,pending=pending.split('\n',1)
                    command=command.strip(); commands.append(command)
                    if command=='exit':return
                    response=command+'\r\n'
                    if command=='show running-config':response+='hostname edge\r\ninterface Ethernet1\r\n description fixture\r\nend\r\n'
                    elif command=='show startup-config':response+='hostname edge-startup\r\nend\r\n'
                    channel.send((response+'edge#').encode())
        except (EOFError,OSError,paramiko.SSHException):
            pass
        finally:
            if transport:transport.close()
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    yield port,path,commands
    stopped.set(); listener.close(); thread.join(12)


@pytest.mark.parametrize('config_type,expected',[('running','hostname edge\n'),('startup','hostname edge-startup\n')])
def test_real_ssh_capture_uses_trust_and_selected_command(ssh_cli,config_type,expected):
    port,_,commands=ssh_cli
    platform,result=collection.fetch_config('127.0.0.1','cisco_ios','fixture','fixture','',port,config_type)
    assert platform=='cisco_ios' and expected in result and result.endswith('end\n')
    assert 'show '+config_type+'-config' in commands
    assert not any(c.startswith('configure') or c.startswith('write') for c in commands)


def test_real_ssh_rejects_untrusted_host_key(ssh_cli):
    port,path,_=ssh_cli
    path.write_text('')
    with pytest.raises(Exception):
        collection.fetch_config('127.0.0.1','cisco_ios','fixture','fixture','',port)
