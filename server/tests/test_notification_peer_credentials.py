from uuid import UUID,uuid4
from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.api.v1 import notification_sites as api
from app.core.config import get_settings
from app.core.crypto import decrypt_config
from test_alert_delivery_keys import world,session  # noqa: F401
from pg_fixture import pg_cluster,pg_db  # noqa: F401


@pytest.fixture
def lifecycle(world,monkeypatch):
    monkeypatch.setattr(get_settings(),'NOTIFICATION_SITE_ROUTING_ENABLED',True)
    monkeypatch.setattr(get_settings(),'SNMP_ENC_KEY','ab'*32)
    async def audit(*args):pass
    monkeypatch.setattr(api,'audit',audit)
    return world


def request(token):
    return Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())]})


async def issue(w):
    async with session(w.db) as db:
        return await api.trust(api.Trust(name='fixture origin',site_id=uuid4(),channel_map={uuid4():UUID(w.ch.noc)}),db,None)


async def authenticates(w,token):
    async with session(w.db) as db:
        try:await api.principal(request(token),db);return True
        except HTTPException as exc:
            assert exc.status_code==401
            return False


@pytest.mark.asyncio
async def test_rotation_overlap_expiry_and_redaction(lifecycle):
    w=lifecycle;original=await issue(w)
    assert await authenticates(w,original['token'])
    async with session(w.db) as db:
        replacement=await api.rotate(original['id'],api.Rotation(),db,None)
    assert await authenticates(w,original['token'])
    assert await authenticates(w,replacement['token'])
    w.db.execute("UPDATE notification_site_peers SET previous_valid_until=clock_timestamp()-interval '1 second'")
    assert not await authenticates(w,original['token'])
    assert await authenticates(w,replacement['token'])
    async with session(w.db) as db:config=await api.config(db,None)
    peer=config['peers'][0]
    assert not any('hash' in key or key=='token' for key in peer)
    assert peer['credential_expires_at'] is not None


@pytest.mark.asyncio
async def test_revocation_survives_enable_and_new_rotation_requires_explicit_enable(lifecycle):
    w=lifecycle;original=await issue(w)
    async with session(w.db) as db:
        replacement=await api.rotate(original['id'],api.Rotation(),db,None)
    async with session(w.db) as db:await api.revoke(original['id'],db,None)
    async with session(w.db) as db:await api.enable(original['id'],db,None)
    assert not await authenticates(w,original['token'])
    assert not await authenticates(w,replacement['token'])
    async with session(w.db) as db:await api.disable(original['id'],db,None)
    async with session(w.db) as db:new=await api.rotate(original['id'],api.Rotation(),db,None)
    assert not await authenticates(w,new['token'])
    async with session(w.db) as db:await api.enable(original['id'],db,None)
    assert await authenticates(w,new['token'])


@pytest.mark.asyncio
async def test_expired_credential_never_gets_overlap_and_second_rotation_replaces_previous(lifecycle):
    w=lifecycle;original=await issue(w)
    w.db.execute("UPDATE notification_site_peers SET credential_expires_at=clock_timestamp()-interval '1 second'")
    assert not await authenticates(w,original['token'])
    async with session(w.db) as db:new=await api.rotate(original['id'],api.Rotation(),db,None)
    assert not await authenticates(w,original['token'])
    async with session(w.db) as db:latest=await api.rotate(original['id'],api.Rotation(overlap_seconds=0),db,None)
    assert not await authenticates(w,new['token'])
    assert await authenticates(w,latest['token'])


@pytest.mark.asyncio
async def test_connection_update_preserves_policy_and_rejects_wrong_identity(lifecycle,monkeypatch):
    w=lifecycle;pid=uuid4();sid=uuid4()
    w.db.execute("INSERT INTO notification_site_peers(id,name,site_id,direction) VALUES($1,'sender',$2,'outbound')",pid,sid)
    w.db.execute("INSERT INTO notification_site_policies(channel_id,senders) VALUES($1,$2::jsonb)",UUID(w.ch.noc),[str(pid),None])
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):return NS(status_code=200,json=lambda:{'site_id':str(sid)})
    monkeypatch.setattr(api.service,'transport',lambda cfg:Client())
    data=api.Peer(name='sender',site_id=sid,url='https://sender.invalid',token=str(uuid4())+'.'+'x'*64)
    async with session(w.db) as db:await api.connection(pid,data,db,None)
    assert w.db.scalar('SELECT senders FROM notification_site_policies')==[str(pid),None]
    assert decrypt_config(w.db.scalar('SELECT config FROM notification_site_peers'))['token']==data.token
    data.site_id=uuid4()
    async with session(w.db) as db:
        with pytest.raises(HTTPException) as exc:await api.connection(pid,data,db,None)
        assert exc.value.status_code==400
