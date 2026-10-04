"""Transient SSH fixtures for the installed collector; no device credentials."""
import io
import json
import logging
import socket
import tempfile
import threading
import time
from pathlib import Path
import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.services.ncm_collection import fetch_config, probe_host_key
from app.services.ncm_errors import classify

logging.getLogger('paramiko').addHandler(logging.NullHandler())
logging.getLogger('paramiko').propagate = False
logging.getLogger('paramiko').setLevel(logging.CRITICAL)

def check(name, multiple, changed_key=False):
    rsa = paramiko.RSAKey.generate(2048)
    raw = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    ed = paramiko.Ed25519Key.from_private_key(io.StringIO(raw))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(8)
    port = listener.getsockname()[1]
    server_errors = []
    peer_keys = []
    def serve():
        transport = None
        try:
            peer, _ = listener.accept()
            transport = paramiko.Transport(peer)
            transport.get_security_options().kex = ('diffie-hellman-group14-sha256', 'diffie-hellman-group14-sha1')
            transport.add_server_key(rsa)
            if multiple:
                transport.add_server_key(ed)
            else:
                transport.get_security_options().key_types = ('ssh-rsa',)
            transport.start_server(server=paramiko.ServerInterface())
            peer_keys.append(transport.host_key_type)
            transport.accept(3)
        except Exception as error:
            server_errors.append(type(error).__name__)
        finally:
            if transport:
                transport.close()
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='nccm-verification-') as temp:
        trust = Path(temp) / 'known_hosts'
        trusted_rsa = paramiko.RSAKey.generate(2048) if changed_key else rsa
        trust.write_text(f'[127.0.0.1]:{port} ssh-rsa {trusted_rsa.get_base64()}\n')
        try:
            fetch_config('127.0.0.1', 'fortinet' if multiple else 'cisco_ios',
                'fixture', 'fixture-only', '', port, known_hosts=str(trust))
            result = {'case': name, 'unexpected_success': True}
        except Exception as error:
            failure = classify(error, 'collection')
            context = error.__cause__ or error.__context__
            result = {'case': name, 'exception': type(error).__name__,
                'underlying_exception': type(context).__name__ if context else None,
                'category': failure.category, 'retryable': failure.retryable,
                'duration_seconds': round(time.monotonic() - began, 3)}
    thread.join(4)
    result.update(server_selected_key=peer_keys, server_errors=server_errors)
    print(json.dumps(result))
    expected='host_key_changed' if changed_key else 'authentication' if multiple else 'negotiation'
    assert result.get('category')==expected and result.get('retryable') is False,result
    if multiple:assert all(k.startswith('rsa-sha2-') for k in peer_keys),result

def test_legacy_negotiation():
    check('Cisco legacy RSA/SHA1 host signature', False)

def test_trusted_rsa_selection():
    check('Fortinet stable RSA + Ed25519 server with RSA trusted', True)

def test_actual_host_key_rotation():
    check('Actual RSA host-key rotation', True, changed_key=True)

def readonly_fortinet():
    raw = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(raw))
    rsa = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(15)
    port = listener.getsockname()[1]
    commands = []
    failures = []
    selected = []
    class ReadOnlyServer(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL
        def get_allowed_auths(self, username):
            return 'password'
        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED
        def check_channel_pty_request(self, *args):
            return True
        def check_channel_shell_request(self, channel):
            return True
    def serve():
        transport = None
        try:
            peer, _ = listener.accept()
            transport = paramiko.Transport(peer)
            transport.get_security_options().kex = ('diffie-hellman-group14-sha256', 'diffie-hellman-group14-sha1')
            transport.add_server_key(key)
            transport.add_server_key(rsa)
            transport.start_server(server=ReadOnlyServer())
            selected.append(transport.host_key_type)
            channel = transport.accept(12)
            channel.settimeout(15)
            channel.send('LAB-FGT $')
            pending = ''
            paging = False
            while True:
                data = channel.recv(1024).decode()
                if not data:
                    break
                if paging and ' ' in data:
                    paging = False
                    channel.send('\r\nend\r\nLAB-FGT $\r\n')
                    continue
                pending += data.replace('\r', '\n')
                while '\n' in pending:
                    command, pending = pending.split('\n', 1)
                    command = command.strip()
                    if not command:
                        channel.send('\r\nLAB-FGT $')
                    elif command == 'show full-configuration':
                        commands.append(command)
                        channel.send('show full-configuration\r\nconfig system global\r\n    set hostname "LAB-FGT"\r\n--More--')
                        paging = True
                    elif command == 'exit':
                        commands.append(command)
                        return
                    else:
                        commands.append(command)
                        channel.send('\r\nCommand fail. Return code -61\r\nLAB-FGT $\r\n')
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            if transport:
                transport.close()
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='nccm-verification-') as temp:
        trust = Path(temp) / 'known_hosts'
        trust.write_text(f'[127.0.0.1]:{port} ssh-rsa {rsa.get_base64()}\n')
        try:
            platform, content = fetch_config('127.0.0.1', 'fortinet', 'fixture',
                'fixture-only', '', port, known_hosts=str(trust))
            result = {'case':'Read-only Fortinet dollar prompt with paging',
                'validated_by_collector':True, 'platform':platform, 'lines':len(content.splitlines())}
        except Exception as error:
            failure = classify(error)
            result = {'case':'Read-only Fortinet dollar prompt with paging',
                'validated_by_collector':False,'exception':type(error).__name__, 'category':failure.category}
    thread.join(4)
    result.update(observed_commands=commands,server_errors=failures,server_selected_key=selected)
    print(json.dumps(result))
    assert result['validated_by_collector'] and commands==['show full-configuration','exit'] and not failures,result
    assert all(k.startswith('rsa-sha2-') for k in selected),result

def test_readonly_fortinet_paging():
    readonly_fortinet()

from netmiko.exceptions import NetmikoTimeoutException
from paramiko.ssh_exception import IncompatiblePeer
def test_wrapped_exception_categories():
    for label,cause,category,retryable in (
        ('nested negotiation',IncompatiblePeer('fixture'),'negotiation',False),
        ('wrapped missing trust',paramiko.SSHException('Server not found in known_hosts'),'host_key_untrusted',False),
        ('actual socket timeout',socket.timeout(),'timeout',True),
    ):
        error=NetmikoTimeoutException('fixture-wrapper')
        error.__context__=cause
        failure=classify(error)
        assert (failure.category,failure.retryable)==(category,retryable),(label,failure)
        print(json.dumps({'case':label,'category':failure.category,'retryable':failure.retryable}))
