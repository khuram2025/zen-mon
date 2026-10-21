"""SSH host key trust: confirm-on-first-use for the terminal, warn-only
first-use recording for NCCM backups (VAPT ZP-01)."""
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import paramiko
import pytest

from app.services import ssh_host_keys, ssh_terminal

KEY = paramiko.ECDSAKey.generate()
OTHER = paramiko.ECDSAKey.generate()


@pytest.fixture
def sshd():
    """A local SSH server that accepts fixture/fixture and opens a shell,
    for as many connections as the test makes."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(4); listener.settimeout(.2)
    stopped = threading.Event()
    auth_attempts = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            auth_attempts.append(username)
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('fixture', 'fixture') else paramiko.AUTH_FAILED
        def get_allowed_auths(self, username): return 'password'
        def check_channel_request(self, kind, chanid): return paramiko.OPEN_SUCCEEDED
        def check_channel_pty_request(self, *args): return True
        def check_channel_shell_request(self, *args): return True

    def serve():
        transports = []
        while not stopped.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = paramiko.Transport(sock); t.add_server_key(KEY)
            transports.append(t)
            try:
                t.start_server(server=Server())
            except (EOFError, OSError, paramiko.SSHException):
                pass
        for t in transports:
            t.close()

    thread = threading.Thread(target=serve, daemon=True); thread.start()
    yield listener.getsockname()[1], auth_attempts
    stopped.set(); listener.close(); thread.join(5)


def _open(port, known=(), confirmed=None):
    client, channel, accepted = ssh_terminal._connect('127.0.0.1', port, 'fixture', 'fixture', 80, 24,
                                                      known, confirmed)
    channel.close(); client.close()
    return accepted


def test_unknown_key_is_refused_before_the_password_is_sent(sshd):
    port, auth_attempts = sshd
    with pytest.raises(ssh_terminal.HostKeyUnknown) as exc:
        _open(port)
    assert exc.value.fingerprint == ssh_host_keys.fingerprint(KEY.get_name(), KEY.get_base64())
    assert auth_attempts == []
    error = ssh_terminal._safe_error(exc.value)
    assert error.status == 409 and error.detail['code'] == 'host_key_unknown'
    assert error.detail['fingerprint'] == exc.value.fingerprint


def test_confirmed_fingerprint_connects_and_is_returned_for_storage(sshd):
    port, auth_attempts = sshd
    fp = ssh_host_keys.fingerprint(KEY.get_name(), KEY.get_base64())
    assert _open(port, confirmed=fp) == (KEY.get_name(), KEY.get_base64())
    assert auth_attempts == ['fixture']


def test_wrong_confirmation_is_refused(sshd):
    port, auth_attempts = sshd
    with pytest.raises(ssh_terminal.HostKeyUnknown):
        _open(port, confirmed=ssh_host_keys.fingerprint(OTHER.get_name(), OTHER.get_base64()))
    assert auth_attempts == []


def test_stored_key_connects_without_a_prompt(sshd):
    port, _ = sshd
    assert _open(port, known=[(KEY.get_name(), KEY.get_base64())]) is None


def test_changed_key_is_refused_even_when_confirmed(sshd):
    port, auth_attempts = sshd
    fp = ssh_host_keys.fingerprint(KEY.get_name(), KEY.get_base64())
    with pytest.raises(paramiko.BadHostKeyException) as exc:
        _open(port, known=[(OTHER.get_name(), OTHER.get_base64())], confirmed=fp)
    assert auth_attempts == []
    error = ssh_terminal._safe_error(exc.value)
    assert error.status == 409 and error.detail['code'] == 'host_key_changed'
    assert error.detail['fingerprint'] == fp
    assert error.detail['previous_fingerprint'] == ssh_host_keys.fingerprint(OTHER.get_name(), OTHER.get_base64())


def test_compare():
    known = [('ssh-ed25519', 'AAAA'), ('ecdsa-sha2-nistp256', KEY.get_base64())]
    assert ssh_host_keys.compare(known, 'ecdsa-sha2-nistp256', KEY.get_base64()) == ('match', None)
    status, previous = ssh_host_keys.compare(known, 'ecdsa-sha2-nistp256', OTHER.get_base64())
    assert status == 'changed' and previous == ssh_host_keys.fingerprint('ecdsa-sha2-nistp256', KEY.get_base64())
    assert ssh_host_keys.compare(known, 'ssh-rsa', 'AAAB') == ('unknown', None)


@pytest.mark.asyncio
async def test_backup_trust_records_first_use_and_only_warns_on_change(monkeypatch):
    observed = {'type': KEY.get_name(), 'base64': KEY.get_base64()}
    record = AsyncMock()
    monkeypatch.setattr(ssh_host_keys, 'record', record)

    monkeypatch.setattr(ssh_host_keys, 'active_keys', AsyncMock(return_value=[]))
    first = await ssh_host_keys.observe_backup_key(None, 'dev', '192.0.2.1', 22, observed)
    assert first['status'] == 'first_use'
    assert record.await_args.kwargs['method'] == ssh_host_keys.FIRST_USE

    record.reset_mock()
    monkeypatch.setattr(ssh_host_keys, 'active_keys',
                        AsyncMock(return_value=[(OTHER.get_name(), OTHER.get_base64())]))
    changed = await ssh_host_keys.observe_backup_key(None, 'dev', '192.0.2.1', 22, observed)
    assert changed['status'] == 'changed' and changed['previous_fingerprint']
    record.assert_not_awaited()  # the trusted key is kept; the change is reported, never adopted

    assert await ssh_host_keys.observe_backup_key(None, 'dev', '192.0.2.1', 22, {}) is None


def test_udt_gateway_enforcement_ignores_recorded_keys():
    """The UDT gateway collector enforces operator-registered keys only, so
    keys recorded by the terminal or by NCCM do not change its behaviour."""
    import inspect
    from app.services import udt_gateway_service
    source = inspect.getsource(udt_gateway_service)
    assert "verification_method NOT IN ('first_use','terminal_confirmed')" in source
