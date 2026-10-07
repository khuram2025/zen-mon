"""SSH test terminal against a loopback SSH server and a private Redis."""
import asyncio
import base64
import json
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace as NS
from uuid import uuid4

import paramiko
import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.services import ssh_terminal as term

pytestmark = pytest.mark.skipif(not shutil.which('redis-server'), reason='redis-server not installed')


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def redis_server(monkeypatch):
    port = _free_port()
    proc = subprocess.Popen(['redis-server', '--port', str(port), '--bind', '127.0.0.1', '--save', '', '--appendonly', 'no'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = aioredis.from_url(f'redis://127.0.0.1:{port}/0', decode_responses=True)
    for _ in range(50):
        try:
            await client.ping()
            break
        except Exception:
            await asyncio.sleep(0.1)
    monkeypatch.setattr(term, '_redis', client)
    yield client
    await client.aclose()
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def ssh_server():
    """Echo shell: prints 'ready$ ' then each line back as 'got:<line>'. 'exit' ends it."""
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(5); listener.settimeout(.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    seen = {'resizes': [], 'logins': []}

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            seen['logins'].append((username, password))
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('lab', 'labpw') else paramiko.AUTH_FAILED
        def get_allowed_auths(self, username): return 'password'
        def check_channel_request(self, kind, chanid): return paramiko.OPEN_SUCCEEDED
        def check_channel_pty_request(self, *a): return True
        def check_channel_shell_request(self, ch): return True
        def check_channel_window_change_request(self, ch, w, h, pw, ph):
            seen['resizes'].append((w, h)); return True

    def handle(sock):
        transport = paramiko.Transport(sock)
        try:
            transport.add_server_key(key)
            transport.start_server(server=Server())
            ch = transport.accept(10)
            if ch is None:
                return
            ch.send(b'ready$ ')
            pending = b''
            while not stop.is_set():
                try:
                    data = ch.recv(1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                pending += data
                while b'\n' in pending or b'\r' in pending:
                    line, pending = pending.replace(b'\r', b'\n').split(b'\n', 1)
                    if line.strip() == b'exit':
                        ch.send(b'bye\r\n'); return
                    ch.send(b'got:' + line + b'\r\nready$ ')
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def serve():
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(sock,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield port, seen
    stop.set(); listener.close()


async def collect(session_id, user, until, timeout=10):
    text, last, closed = '', '0', None
    deadline = time.monotonic() + timeout
    agen = term.stream(session_id, user, last)
    try:
        async for line in agen:
            msg = json.loads(line)
            if msg.get('d'):
                text += base64.b64decode(msg['d']).decode(errors='replace')
            if msg.get('e') == 'closed':
                closed = msg['r']; break
            if until in text or time.monotonic() > deadline:
                break
    finally:
        await agen.aclose()
    return text, closed


def b64(s):
    return base64.b64encode(s.encode()).decode()


@pytest.mark.asyncio
async def test_open_type_resize_and_close(redis_server, ssh_server):
    port, seen = ssh_server
    user = NS(id=uuid4())
    sid = await term.open_session(user, '127.0.0.1', port, 'lab', 'labpw', 100, 30)   # no host key setup needed
    text, _ = await collect(sid, user, 'ready$ ')
    assert 'ready$ ' in text
    await term.send_input(sid, user, {'t': 'd', 'd': b64('hello\n')})
    text, _ = await collect(sid, user, 'got:hello')
    assert 'got:hello' in text
    await term.send_input(sid, user, {'t': 'r', 'cols': 90, 'rows': 40})
    await asyncio.sleep(1)
    assert (90, 40) in seen['resizes']
    await term.close_session(sid, user)
    _, closed = await collect(sid, user, 'never')
    assert closed == 'user_closed'
    assert await term._active_count(str(user.id)) == 0


@pytest.mark.asyncio
async def test_remote_exit_ends_the_stream(redis_server, ssh_server):
    port, _ = ssh_server
    user = NS(id=uuid4())
    sid = await term.open_session(user, '127.0.0.1', port, 'lab', 'labpw', 80, 24)
    await term.send_input(sid, user, {'t': 'd', 'd': b64('exit\n')})
    text, closed = await collect(sid, user, 'never')
    assert 'bye' in text and closed == 'remote_closed'


@pytest.mark.asyncio
async def test_wrong_password_and_unreachable_have_safe_messages(redis_server, ssh_server):
    port, _ = ssh_server
    user = NS(id=uuid4())
    with pytest.raises(term.TerminalError) as bad:
        await term.open_session(user, '127.0.0.1', port, 'lab', 'wrong', 80, 24)
    assert bad.value.status == 422 and 'wrong' not in str(bad.value)
    with pytest.raises(term.TerminalError) as down:
        await term.open_session(user, '127.0.0.1', _free_port(), 'lab', 'labpw', 80, 24)
    assert down.value.status == 502


@pytest.mark.asyncio
async def test_sessions_are_private_and_limited(redis_server, ssh_server):
    port, _ = ssh_server
    owner, other = NS(id=uuid4()), NS(id=uuid4())
    ids = [await term.open_session(owner, '127.0.0.1', port, 'lab', 'labpw', 80, 24) for _ in range(term.MAX_PER_USER)]
    with pytest.raises(term.TerminalError) as limited:
        await term.open_session(owner, '127.0.0.1', port, 'lab', 'labpw', 80, 24)
    assert limited.value.status == 429
    for call in (term.send_input(ids[0], other, {'t': 'd', 'd': b64('x')}), term.close_session(ids[0], other)):
        with pytest.raises(term.TerminalError) as denied:
            await call
        assert denied.value.status == 404
    for sid in ids:
        await term.close_session(sid, owner)
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_password_is_not_stored_in_redis(redis_server, ssh_server):
    port, _ = ssh_server
    user = NS(id=uuid4())
    sid = await term.open_session(user, '127.0.0.1', port, 'lab', 'labpw', 80, 24)
    dump = ''
    for key in await redis_server.keys('zp:sshterm:*'):
        kind = await redis_server.type(key)
        reader = {'hash': redis_server.hgetall, 'stream': lambda k: redis_server.xrange(k), 'list': lambda k: redis_server.lrange(k, 0, -1),
                  'set': redis_server.smembers, 'string': redis_server.get}[kind]
        dump += key + str(await reader(key))
    assert 'labpw' not in dump
    await term.close_session(sid, user)
    await asyncio.sleep(1)
