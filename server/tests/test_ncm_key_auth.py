"""SSH-key connection profiles with the bundled Netmiko 4.7.0 / Paramiko 4.0.

Netmiko's BaseConnection runs ``_key_check`` whenever ``use_keys=True`` and
raises ValueError unless ``key_file`` names an existing file, so the previous
``use_keys=True, pkey=<in-memory key>`` call failed before any connection.
The collector now passes the in-memory key as ``pkey`` with use_keys=False
(Paramiko always tries ``pkey``); these tests run the real Netmiko classes,
including a full login and a paginated FortiOS capture against a local
Paramiko server, and check that key problems are reported specifically.
"""
import io
import logging
import socket
import threading

import netmiko
import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from netmiko import ConnectHandler

from app.services import ncm_collection as collection
from app.services.ncm_collection import KeyLoadError, KeyRejectedError, key_connect_args, load_private_key
from app.services.ncm_errors import classify, GUIDANCE, STAGES

logging.getLogger('paramiko').addHandler(logging.NullHandler())
logging.getLogger('paramiko').propagate = False
logging.getLogger('paramiko').setLevel(logging.CRITICAL)


def private_text(key, fmt=serialization.PrivateFormat.OpenSSH, passphrase=None):
    enc = serialization.BestAvailableEncryption(passphrase.encode()) if passphrase else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc).decode()


def ed25519_text(passphrase=None):
    return private_text(ed25519.Ed25519PrivateKey.generate(), passphrase=passphrase)


def test_bundled_versions_are_the_pinned_ones():
    assert netmiko.__version__ == '4.7.0' and paramiko.__version__ == '4.0.0'


def test_netmiko_refuses_in_memory_key_with_use_keys_but_accepts_supported_args():
    key = load_private_key(ed25519_text())
    common = dict(device_type='fortinet', host='192.0.2.1', username='backup', auto_connect=False,
                  disabled_algorithms=collection.ssh_algorithms())
    # Root cause of the field failure: use_keys=True demands key_file.
    with pytest.raises(ValueError, match='key_file'):
        ConnectHandler(**common, use_keys=True, pkey=key)
    conn = ConnectHandler(**common, **key_connect_args(key))     # no ValueError
    params = conn._connect_params_dict()
    assert params['pkey'] is key and params['look_for_keys'] is False and params['key_filename'] is None
    assert params['allow_agent'] is False and params['password'] is None
    # RSA SHA-1 signatures stay disabled for key logins.
    assert 'ssh-rsa' in params['disabled_algorithms']['pubkeys']
    assert key_connect_args(key, 'second-factor')['password'] == 'second-factor'


def test_collector_never_writes_the_key_to_disk(monkeypatch):
    opened = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    load_private_key(ed25519_text('correct horse'), 'correct horse')
    assert opened == []


@pytest.mark.parametrize('make,passphrase,reason', [
    (lambda: ed25519_text('secret-pass'), None, 'key_passphrase_required'),
    (lambda: ed25519_text('secret-pass'), 'wrong-pass', 'key_passphrase_wrong'),
    (lambda: private_text(rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.TraditionalOpenSSL,
                          'secret-pass'), None, 'key_passphrase_required'),
    (lambda: private_text(rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.PKCS8,
                          'secret-pass'), 'wrong-pass', 'key_passphrase_wrong'),
    (lambda: private_text(ec.generate_private_key(ec.SECP256K1()), serialization.PrivateFormat.PKCS8),
     None, 'key_unsupported'),
    (lambda: 'PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\n', None, 'key_unsupported'),
    (lambda: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0 backup@host', None, 'key_format'),
    (lambda: '-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----', None, 'key_format'),
    (lambda: 'not a key', None, 'key_format'),
])
def test_key_loading_failures_are_specific(make, passphrase, reason):
    with pytest.raises(KeyLoadError) as exc:
        load_private_key(make(), passphrase)
    assert exc.value.reason == reason
    failure = classify(exc.value, 'key')
    assert failure.category == 'key' and failure.retryable is False
    assert failure.message == str(exc.value) and 'Collection failed' not in failure.message
    # Even at the old credentials stage the key cause wins over the generic text.
    assert classify(exc.value, 'credentials').category == 'key'
    assert GUIDANCE['key'] and STAGES['key'] == 'SSH key loading'
    assert 'PRIVATE KEY' not in failure.message.replace('PRIVATE key', '')


@pytest.mark.parametrize('make,passphrase', [
    (lambda: ed25519_text(), None), (lambda: ed25519_text('pw-12345'), 'pw-12345'),
    (lambda: ed25519_text(), 'ignored-for-unencrypted'),
    (lambda: private_text(rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.TraditionalOpenSSL), None),
    (lambda: private_text(rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.PKCS8, 'pw-12345'), 'pw-12345'),
    (lambda: private_text(ec.generate_private_key(ec.SECP384R1()), serialization.PrivateFormat.TraditionalOpenSSL), None),
])
def test_supported_keys_load(make, passphrase):
    assert isinstance(load_private_key(make(), passphrase), paramiko.PKey)


def test_profile_save_reports_key_cause_as_422():
    from fastapi import HTTPException
    from app.api.v1.ncm import CredentialIn, _parse_key
    data = CredentialIn(name='p', username='u', auth_method='key', private_key=ed25519_text('pw-12345'))
    with pytest.raises(HTTPException) as exc:
        _parse_key(data)
    assert exc.value.status_code == 422 and 'passphrase' in exc.value.detail


# --------------------------------------------------------------------------- #
# Real Netmiko login + paginated FortiOS capture against a local SSH server
# --------------------------------------------------------------------------- #
def fortios_config(blocks):
    lines = ['config system global', '    set hostname "FGT201F"', 'end']
    for b in range(blocks):
        lines += ['config firewall address']
        for i in range(50):
            lines += [f'    edit "net-{b}-{i}"', f'        set subnet 10.{b % 256}.{i}.0 255.255.255.0', '    next']
        lines += ['end']
    return '\n'.join(lines) + '\n'


class FortiServer:
    """Paramiko SSH server: one shell session emulating FortiOS paging."""

    def __init__(self, accepted_key=None, allowed='publickey', config='', page_lines=80):
        self.accepted = accepted_key
        self.allowed = allowed
        self.config = config
        self.page_lines = page_lines
        self.auth_attempts = []
        self.commands = []
        self.errors = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(20)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        outer = self

        class Server(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return outer.allowed

            def check_auth_publickey(self, username, key):
                outer.auth_attempts.append(('publickey', key.get_name()))
                ok = outer.accepted is not None and key.get_base64() == outer.accepted.get_base64()
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_auth_password(self, username, password):
                outer.auth_attempts.append(('password', None))
                return paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED

            def check_channel_pty_request(self, *args):
                return True

            def check_channel_shell_request(self, channel):
                return True
        transport = None
        try:
            peer, _ = self.listener.accept()
            peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            transport = paramiko.Transport(peer)
            transport.add_server_key(paramiko.Ed25519Key.from_private_key(io.StringIO(ed25519_text())))
            transport.start_server(server=Server())
            channel = transport.accept(15)
            if channel is None:
                return
            channel.settimeout(20)
            self.shell(channel)
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            if transport:
                transport.close()
            self.listener.close()

    def shell(self, channel):
        prompt = 'FGT201F # '
        channel.send(prompt)
        lines = self.config.replace('\n', '\r\n').split('\r\n')
        pages = ['\r\n'.join(lines[i:i + self.page_lines]) for i in range(0, len(lines), self.page_lines)]
        page = None
        pending = ''
        while True:
            data = channel.recv(4096).decode()
            if not data:
                return
            if page is not None and ' ' in data:
                text = '\r         \r' + pages[page]          # FortiOS erases the pager line
                page += 1
                if page < len(pages):
                    channel.send(text + '\r\n--More-- ')
                else:
                    page = None
                    channel.send(text + '\r\n' + prompt)
                continue
            pending += data.replace('\r', '\n')
            while '\n' in pending:
                command, pending = pending.split('\n', 1)
                command = command.strip()
                if not command:
                    channel.send('\r\n' + prompt)
                    continue
                self.commands.append(command)
                if command == 'show full-configuration':
                    channel.send('show full-configuration\r\n' + pages[0] + '\r\n--More-- ')
                    page = 1
                elif command == 'exit':
                    return
                else:
                    channel.send('\r\nCommand fail. Return code -61\r\n' + prompt)

    def join(self):
        self.thread.join(10)


@pytest.mark.parametrize('kind', ['ed25519', 'rsa'])
def test_key_profile_logs_in_and_captures_paginated_fortios(kind):
    if kind == 'rsa':
        text = private_text(rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.TraditionalOpenSSL)
    else:
        text = ed25519_text('pw-12345')
    key = load_private_key(text, 'pw-12345' if kind == 'ed25519' else None)
    config = fortios_config(120)                          # ~0.5 MB, ~250 pages
    server = FortiServer(accepted_key=key, config=config, page_lines=80)
    progress = collection.CaptureProgress()
    platform, content = collection.fetch_config('127.0.0.1', 'fortinet', 'backup', '', '', server.port,
                                                pkey=key, progress=progress)
    server.join()
    assert platform == 'fortinet' and content.rstrip('\n') == config.rstrip('\n')
    assert server.commands == ['show full-configuration', 'exit'] and not server.errors
    # Key only: no password attempt (lockout counters) and no agent/disk keys.
    assert server.auth_attempts and all(method == 'publickey' for method, _ in server.auth_attempts)
    assert progress.snapshot()['pages'] == len(range(0, len(config.split('\n')), 80)) - 1
    assert progress.snapshot()['deadline_seconds'] == 900


def test_rejected_key_is_reported_as_key_rejection():
    key = load_private_key(ed25519_text())
    server = FortiServer(accepted_key=load_private_key(ed25519_text()))
    with pytest.raises(KeyRejectedError) as exc:
        collection.fetch_config('127.0.0.1', 'fortinet', 'backup', '', '', server.port, pkey=key)
    server.join()
    failure = classify(exc.value, 'collection')
    assert failure.category == 'authentication' and not failure.retryable
    assert 'rejected the SSH key' in failure.message


def test_device_without_publickey_login_is_named():
    key = load_private_key(ed25519_text())
    server = FortiServer(allowed='password')
    with pytest.raises(KeyRejectedError, match='does not accept SSH key login .*offers: password'):
        collection.fetch_config('127.0.0.1', 'fortinet', 'backup', '', '', server.port, pkey=key)
    server.join()
