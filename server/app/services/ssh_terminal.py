"""Interactive SSH shell for the Settings > Monitoring test terminal.

Plain HTTP, no WebSocket: nginx on the appliance does not forward upgrades and
the API runs several workers. The worker that opens a session owns the SSH
connection; every other worker talks to it through Redis (an input list and an
output stream), so any worker can serve any request for a session.

Credentials are used once to authenticate and are never stored or logged. The
device host key is not checked (the same policy as NCCM collection).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import threading
import time
import secrets
from dataclasses import dataclass, field

import paramiko
import redis.asyncio as aioredis
from paramiko.ssh_exception import NoValidConnectionsError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

IDLE_SECONDS = 600          # no keystrokes for 10 minutes
MAX_SECONDS = 3600          # hard cap per session
MAX_PER_USER = 3
MAX_INPUT_BYTES = 32 * 1024
MAX_INPUT_BACKLOG = 2000
OUT_MAXLEN = 4000
READ_CHUNK = 4096
ALIVE_TTL = 15
KEY_TTL_AFTER_CLOSE = 120

_redis: aioredis.Redis | None = None


def redis_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True,
                                   socket_connect_timeout=2)
    return _redis


def _k(session_id: str, part: str) -> str:
    return f'zp:sshterm:{session_id}:{part}'


def _user_key(user_id) -> str:
    return f'zp:sshterm:user:{user_id}'


class TerminalError(Exception):
    """A failure whose message is safe to show to the operator."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class _Session:
    id: str
    user_id: str
    client: paramiko.SSHClient
    channel: paramiko.Channel
    started: float = field(default_factory=time.monotonic)
    last_input: float = field(default_factory=time.monotonic)
    closing: bool = False
    reason: str = 'closed'
    out: asyncio.Queue = field(default_factory=asyncio.Queue)


# Sessions owned by THIS worker process.
_owned: dict[str, _Session] = {}


def _connect(host, port, username, password, cols, rows):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password,
                       allow_agent=False, look_for_keys=False,
                       timeout=15, banner_timeout=15, auth_timeout=15)
        channel = client.invoke_shell(term='xterm-256color', width=cols, height=rows)
        channel.settimeout(0.5)
        return client, channel
    except BaseException:
        client.close()
        raise


def _safe_error(exc: Exception) -> TerminalError:
    if isinstance(exc, paramiko.AuthenticationException):
        return TerminalError('Authentication failed: check the username and password.', 422)  # never 401: the SPA treats that as an expired login
    if isinstance(exc, (NoValidConnectionsError, ConnectionRefusedError)):
        return TerminalError('Connection refused: SSH is not listening on that address and port.', 502)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return TerminalError('Connection timed out: the host did not answer.', 504)
    if isinstance(exc, socket.gaierror):
        return TerminalError('The host name could not be resolved.', 502)
    if isinstance(exc, paramiko.SSHException):
        return TerminalError('SSH negotiation failed: the device and the appliance share no SSH algorithms.', 502)
    if isinstance(exc, OSError):
        return TerminalError('Unable to reach the host: check routing and firewall rules.', 502)
    return TerminalError('The SSH session could not be opened.', 502)


async def _active_count(user_id) -> int:
    r = redis_client()
    ids = await r.smembers(_user_key(user_id))
    alive = 0
    for sid in ids:
        if await r.exists(_k(sid, 'meta')):
            alive += 1
        else:
            await r.srem(_user_key(user_id), sid)
    return alive


async def open_session(user, host: str, port: int, username: str, password: str,
                       cols: int, rows: int) -> str:
    r = redis_client()
    try:
        await r.ping()
    except Exception:
        raise TerminalError('The terminal service is unavailable (Redis).', 503) from None
    user_id = str(user.id)
    if await _active_count(user_id) >= MAX_PER_USER:
        raise TerminalError(f'Close an existing terminal first (limit {MAX_PER_USER} per user).', 429)
    try:
        client, channel = await asyncio.to_thread(_connect, host, port, username, password, cols, rows)
    except Exception as exc:
        raise _safe_error(exc) from None
    session = _Session(id=secrets.token_urlsafe(24), user_id=user_id, client=client, channel=channel)
    await r.hset(_k(session.id, 'meta'), mapping={'user_id': user_id, 'host': host, 'port': port,
                                                  'username': username, 'started': int(time.time())})
    await r.expire(_k(session.id, 'meta'), MAX_SECONDS + KEY_TTL_AFTER_CLOSE)
    await r.set(_k(session.id, 'alive'), '1', ex=ALIVE_TTL)
    await r.sadd(_user_key(user_id), session.id)
    await r.expire(_user_key(user_id), MAX_SECONDS + KEY_TTL_AFTER_CLOSE)
    _owned[session.id] = session
    loop = asyncio.get_running_loop()
    threading.Thread(target=_reader, args=(session, loop), daemon=True, name='ssh-terminal-reader').start()
    asyncio.create_task(_run(session))
    return session.id


def _reader(session: _Session, loop: asyncio.AbstractEventLoop) -> None:
    """Blocking reads from the SSH channel; hands data to the event loop."""
    channel = session.channel
    try:
        while not session.closing:
            try:
                data = channel.recv(READ_CHUNK)
            except socket.timeout:
                continue
            if not data:
                break
            loop.call_soon_threadsafe(session.out.put_nowait, data)
    except Exception:
        pass
    finally:
        if not session.closing:
            session.reason = 'remote_closed'
        loop.call_soon_threadsafe(session.out.put_nowait, None)


async def _emit(session: _Session, **fields) -> None:
    await redis_client().xadd(_k(session.id, 'out'), fields, maxlen=OUT_MAXLEN, approximate=True)


async def _pump_out(session: _Session) -> None:
    while True:
        data = await session.out.get()
        if data is None:
            session.closing = True
            return
        await _emit(session, d=base64.b64encode(data).decode())


async def _pump_in(session: _Session) -> None:
    r = redis_client()
    in_key = _k(session.id, 'in')
    while not session.closing:
        await r.set(_k(session.id, 'alive'), '1', ex=ALIVE_TTL)
        now = time.monotonic()
        if now - session.started > MAX_SECONDS:
            session.reason, session.closing = 'max_duration', True
            return
        if now - session.last_input > IDLE_SECONDS:
            session.reason, session.closing = 'idle_timeout', True
            return
        item = await r.blpop(in_key, timeout=2)
        if not item:
            continue
        try:
            msg = json.loads(item[1])
        except ValueError:
            continue
        kind = msg.get('t')
        if kind == 'd':
            session.last_input = time.monotonic()
            try:
                await asyncio.to_thread(session.channel.sendall, base64.b64decode(msg.get('d', '')))
            except Exception:
                session.reason, session.closing = 'remote_closed', True
        elif kind == 'r':
            try:
                session.channel.resize_pty(width=int(msg['cols']), height=int(msg['rows']))
            except Exception:
                pass
        elif kind == 'x':
            session.reason, session.closing = 'user_closed', True


async def _run(session: _Session) -> None:
    out_task = asyncio.create_task(_pump_out(session))
    in_task = asyncio.create_task(_pump_in(session))
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        session.closing = True
        for task in (in_task, out_task):
            task.cancel()
        for closer in (session.channel.close, session.client.close):
            try:
                await asyncio.to_thread(closer)
            except Exception:
                pass
        r = redis_client()
        try:
            await _emit(session, e='closed', r=session.reason)
            for part in ('meta', 'out', 'in', 'alive'):
                await r.expire(_k(session.id, part), KEY_TTL_AFTER_CLOSE)
            await r.srem(_user_key(session.user_id), session.id)
            await r.delete(_k(session.id, 'alive'))
        except Exception:
            logger.warning('ssh terminal cleanup failed', exc_info=True)
        _owned.pop(session.id, None)
        try:
            from app.core.database import AsyncSessionLocal
            from app.services.audit_service import write_audit_log
            async with AsyncSessionLocal() as db:
                meta = await r.hgetall(_k(session.id, 'meta'))
                await write_audit_log(db, actor=None, action='ssh_terminal.close', resource_type='ssh_terminal',
                                      resource_id=session.id,
                                      metadata={'reason': session.reason, 'host': meta.get('host'),
                                                'port': meta.get('port'), 'username': meta.get('username'),
                                                'user_id': session.user_id,
                                                'seconds': int(time.monotonic() - session.started)})
                await db.commit()
        except Exception:
            logger.warning('ssh terminal close audit failed', exc_info=True)


async def meta_for(session_id: str, user) -> dict:
    """Session metadata when it exists AND belongs to this user (else 404)."""
    meta = await redis_client().hgetall(_k(session_id, 'meta'))
    if not meta or meta.get('user_id') != str(user.id):
        raise TerminalError('Terminal session not found.', 404)
    return meta


async def send_input(session_id: str, user, payload: dict) -> None:
    await meta_for(session_id, user)
    r = redis_client()
    if await r.llen(_k(session_id, 'in')) > MAX_INPUT_BACKLOG:
        raise TerminalError('The terminal is not keeping up; slow down.', 429)
    await r.rpush(_k(session_id, 'in'), json.dumps(payload))
    await r.expire(_k(session_id, 'in'), MAX_SECONDS)


async def close_session(session_id: str, user) -> None:
    await meta_for(session_id, user)
    await send_input(session_id, user, {'t': 'x'})
    # If the owning worker is gone nobody will process the close: end it here.
    r = redis_client()
    await asyncio.sleep(0.5)
    if await r.exists(_k(session_id, 'alive')) or session_id in _owned:
        return
    last = await r.xrevrange(_k(session_id, 'out'), count=1)
    if not last or last[0][1].get('e') != 'closed':
        await r.xadd(_k(session_id, 'out'), {'e': 'closed', 'r': 'lost'})
    await r.srem(_user_key(str(user.id)), session_id)
    for part in ('meta', 'out', 'in'):
        await r.expire(_k(session_id, part), KEY_TTL_AFTER_CLOSE)


async def stream(session_id: str, user, after: str = '0'):
    """Yield NDJSON lines: {"id","d"} output, {"e":"closed","r"} end, {} keep-alive."""
    await meta_for(session_id, user)
    r = redis_client()
    last = after if after and after != 'undefined' else '0'
    missing_since = None
    while True:
        res = await r.xread({_k(session_id, 'out'): last}, count=100, block=5000)
        if res:
            for entry_id, fields in res[0][1]:
                last = entry_id
                if fields.get('e') == 'closed':
                    yield json.dumps({'id': entry_id, 'e': 'closed', 'r': fields.get('r', 'closed')}) + '\n'
                    return
                yield json.dumps({'id': entry_id, 'd': fields.get('d', '')}) + '\n'
            missing_since = None
            continue
        if not await r.exists(_k(session_id, 'alive')):
            missing_since = missing_since or time.monotonic()
            if time.monotonic() - missing_since > ALIVE_TTL + 5:
                yield json.dumps({'e': 'closed', 'r': 'lost'}) + '\n'
                return
        else:
            missing_since = None
        yield '{}\n'
