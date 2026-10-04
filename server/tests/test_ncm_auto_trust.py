"""First-use policy and real SSH/database fixtures; no installed DB writes."""
import asyncio
import os
import socket
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import paramiko
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.api.v1 import ncm
from app.services import ncm_collection as collection, ncm_readiness as readiness
from app.services.ncm_errors import PrerequisiteError, classify
from app.services.ncm_trust import ensure_trust


def test_profile_default_and_trust_permission():
    data = ncm.CredentialIn(name='fixture', username='fixture')
    assert data.ssh_auto_trust is False
    data.ssh_auto_trust = True
    async def run():
        db = NS(execute=AsyncMock(return_value=NS(first=lambda: NS(private_key_enc=None, ssh_auto_trust=False))))
        with patch.object(ncm, '_global_credentials_access', AsyncMock()), \
             patch.object(ncm, 'user_has_permission', AsyncMock(return_value=False)):
            with pytest.raises(HTTPException) as error:
                await ncm.create_credential(data, db, NS())
            assert error.value.status_code == 403
            db.execute.assert_not_awaited()
            with pytest.raises(HTTPException) as error:
                await ncm.update_credential(uuid4(), data, db, NS())
            assert error.value.status_code == 403
            assert db.execute.await_count == 1  # read only; no profile mutation
    asyncio.run(run())


def test_first_use_state_allows_queue_without_claiming_capture():
    device = dict(device_id='fixture', vendor='Cisco', platform='cisco_ios', config_types=['running'],
                  eligible=True, eligibility_reason='fixture', profile_configured=True, ncm_enabled=True,
                  fresh=False, schedule_enabled=False)
    runtime = dict(worker='active', scheduler='active', delivery='active', encryption='configured')
    result = readiness.device_readiness(device, runtime, dict(state='pending', source='first_use', message='Will learn'))
    assert result['can_capture'] and not result['capture_verified']
    assert next(c for c in result['checks'] if c['id'] == 'trust')['status'] == 'pending'


@pytest.fixture
def first_use_server():
    """Accept probe + repeated logins; replace the key to model rotation/MITM."""
    current = {'key': paramiko.ECDSAKey.generate(), 'connections': 0, 'authentications': 0}
    stopped = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(8); listener.settimeout(.2)
    port = listener.getsockname()[1]
    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            current['authentications'] += 1
            return paramiko.AUTH_SUCCESSFUL
        def get_allowed_auths(self, username): return 'password'
        def check_channel_request(self, kind, chanid): return paramiko.OPEN_SUCCEEDED
        def check_channel_pty_request(self, *args): return True
        def check_channel_shell_request(self, *args): return True
    def serve():
        while not stopped.is_set():
            transport = None
            try:
                try: peer, _ = listener.accept()
                except socket.timeout: continue
                current['connections'] += 1
                transport = paramiko.Transport(peer); transport.add_server_key(current['key'])
                transport.start_server(server=Server())
                channel = transport.accept(1)
                if channel is None: continue  # unauthenticated host-key probe
                channel.settimeout(.2); channel.send('fixture $')
                pending = ''
                exiting = False
                while not stopped.is_set():
                    try: raw = channel.recv(65536)
                    except socket.timeout: continue
                    if not raw: break
                    pending += raw.decode().replace('\r', '\n')
                    while '\n' in pending:
                        command, pending = pending.split('\n', 1)
                        command = command.strip()
                        if command == 'exit':
                            exiting = True
                            break
                        reply = command + '\r\n' if command else '\r\n'
                        if command == 'show full-configuration':
                            reply += 'config system global\r\n    set hostname "fixture"\r\nend\r\n'
                        channel.send((reply + 'fixture $\r\n').encode())
                    if exiting: break
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                if transport: transport.close()
    thread = threading.Thread(target=serve, daemon=True); thread.start()
    yield port, current
    stopped.set(); listener.close(); thread.join(5)


def test_first_use_persistence_strict_login_rotation_retirement_and_verification(first_use_server, tmp_path, monkeypatch):
    dsn = os.environ.get('UDT_TEST_DSN')
    if not dsn: pytest.skip('UDT_TEST_DSN disposable fixture required')
    port, server = first_use_server
    system = tmp_path / 'system_known_hosts'; system.write_text('')
    monkeypatch.setattr(readiness, 'KNOWN_HOSTS', str(system))
    monkeypatch.setattr(collection, 'KNOWN_HOSTS', str(system))
    device_id, profile_id = uuid4(), uuid4()
    async def run():
        engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://', 1))
        async with engine.connect() as conn:
            for ddl in [
                '''CREATE TEMP TABLE ncm_host_keys(id uuid DEFAULT gen_random_uuid(),device_id uuid,host text,
                   port int,key_type text,key_base64 text,fingerprint_sha256 text,verification_method text,
                   verification_note text,registered_by uuid,registered_by_name text,created_at timestamptz DEFAULT now(),
                   retired_at timestamptz,retired_by_name text,retire_reason text)''',
                'CREATE UNIQUE INDEX fixture_ncm_trust ON ncm_host_keys(host,port,key_type) WHERE retired_at IS NULL',
                '''CREATE TEMP TABLE ncm_audit(actor_id uuid,actor_name text,action text,device_id uuid,
                   resource_id text,metadata jsonb)''',
            ]: await conn.execute(text(ddl))
            await conn.commit()
            async with AsyncSession(bind=conn) as db:
                async def learn(auto=True):
                    await ensure_trust(db, '127.0.0.1', port, device_id=device_id,
                                       credential_id=profile_id, auto_trust=auto)
                async def capture():
                    keys = await readiness.managed_keys(db, '127.0.0.1', port)
                    path = collection.known_hosts_file([('127.0.0.1', port, k.key_type, k.key_base64) for k in keys])
                    try:
                        return await asyncio.to_thread(collection.fetch_config, '127.0.0.1', 'fortinet',
                                                       'fixture', 'fixture-only', '', port, known_hosts=path)
                    finally: os.unlink(path)
                assert (await readiness.trust_state(db, '127.0.0.1', port))['state'] == 'blocked'
                assert (await readiness.trust_state(db, '127.0.0.1', port, auto_trust=True))['state'] == 'pending'
                with pytest.raises(PrerequisiteError): await learn(False)
                assert server['connections'] == 0
                await learn()
                assert server['authentications'] == 0  # no credential sent by probe
                assert (await readiness.trust_state(db, '127.0.0.1', port, auto_trust=True))['source'] == 'first_use'
                assert (await readiness.trust_state(db, '127.0.0.1', port))['state'] == 'blocked'
                assert 'hostname "fixture"' in (await capture())[1]
                await db.commit()
                first = (await readiness.managed_keys(db, '127.0.0.1', port))[0]
                assert first.key_base64 == server['key'].get_base64()
                assert (await db.execute(text("SELECT count(*) FROM ncm_audit WHERE action='trust.learn'"))).scalar() == 1
                await learn(); await capture(); await db.commit()
                assert server['connections'] == 3 and server['authentications'] == 2
                # No automatic replacement, including no password sent to changed peer.
                server['key'] = paramiko.ECDSAKey.generate()
                await learn()
                with pytest.raises(Exception) as error: await capture()
                assert classify(error.value, 'collection').category == 'host_key_changed'
                await db.commit()  # failed captures retain first-use pin
                assert server['authentications'] == 2
                assert (await readiness.managed_keys(db, '127.0.0.1', port))[0].key_base64 == first.key_base64
                # Verify the original pin independently through the normal API.
                actor = NS(id=uuid4(), username='fixture administrator')
                with patch.object(ncm, '_device_access', AsyncMock()), \
                     patch.object(ncm, '_trust_target', AsyncMock(return_value=('127.0.0.1', port, 'fixture'))):
                    result = await ncm.register_trust(device_id, ncm.TrustIn(
                        public_key=first.key_type + ' ' + first.key_base64, expected_fingerprint=first.fingerprint_sha256,
                        verification_method='console', verification_note='Fixture independent record'), db, actor)
                assert not result['unchanged']
                assert (await readiness.trust_state(db, '127.0.0.1', port))['state'] == 'pass'
                await db.execute(text('UPDATE ncm_host_keys SET retired_at=now()')); await db.commit()
                before = server['connections']
                with pytest.raises(PrerequisiteError): await learn()
                assert server['connections'] == before
                assert 'retired' in (await readiness.trust_state(db, '127.0.0.1', port, auto_trust=True))['message']
                # Port scoping and malformed trust files fail closed.
                assert (await readiness.trust_state(db, '127.0.0.1', port + 1, auto_trust=True))['state'] == 'pending'
                system.write_text('host ssh-rsa invalid-base64\n')
                assert (await readiness.trust_state(db, '127.0.0.1', port + 1, auto_trust=True))['state'] == 'blocked'
        await engine.dispose()
    asyncio.run(run())
