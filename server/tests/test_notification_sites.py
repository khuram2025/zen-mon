import asyncio
import datetime as dt
from types import SimpleNamespace as NS
from uuid import uuid4,uuid5,UUID
import pytest
from sqlalchemy import text
from app.services import notification_sites as sites,host_alert_service as has
from app.core.config import get_settings
from test_alert_delivery_keys import world,ctx,session  # noqa: F401
from pg_fixture import pg_cluster,pg_db  # noqa: F401


def incoming(w,**extra):
    origin=str(uuid4());cid=str(uuid4());aid=str(uuid4());pid=uuid4()
    w.db.execute("INSERT INTO notification_site_peers(id,name,site_id,direction,channel_map) VALUES($1,'origin',$2,'inbound',$3::jsonb)",pid,UUID(origin),{cid:w.ch.noc})
    peer=NS(id=pid,site_id=UUID(origin),channel_map={cid:w.ch.noc})
    now=dt.datetime.now(dt.timezone.utc)
    env={'origin_site':origin,'alert_id':aid,'event':'trigger','channel_id':cid,
         'recipient':'ops@example.test','payload':ctx(aid),
         'grant_token':str(uuid4()),'issued_at':now.isoformat(),'expires_at':(now+dt.timedelta(seconds=60)).isoformat()}
    env.update(extra)
    env['id']=str(uuid5(UUID(origin),sites.digest([aid,'trigger',cid,env['recipient']])))
    return peer,env


@pytest.mark.asyncio
async def test_foreign_incident_uses_remote_receipt_without_fabricating_a_local_alert(world):
    w=world;peer,env=incoming(w)
    async with session(w.db) as db:
        assert await sites.receive(db,peer,env)=='accepted'
    assert w.mails==['ops@example.test']
    assert w.db.scalar('SELECT count(*) FROM alerts WHERE id=$1',UUID(env['alert_id']))==0
    assert w.db.scalar('SELECT status FROM notification_site_inbox')=='accepted'
    async with session(w.db) as db:
        assert await sites.receive(db,peer,env)=='accepted'
    assert len(w.mails)==1


@pytest.mark.asyncio
async def test_same_grant_replay_cannot_start_concurrent_provider_calls(world):
    w=world;peer,env=incoming(w);w.behaviour.delay=.1
    async def receive():
        async with session(w.db) as db:return await sites.receive(db,peer,env)
    outcomes=await asyncio.gather(receive(),receive())
    assert 'accepted' in outcomes and set(outcomes)<={'accepted','uncertain'}
    assert w.mails==['ops@example.test']


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['recipient','origin','expired','payload'])
async def test_unauthorized_or_expired_envelopes_do_not_send(world,change):
    w=world;peer,env=incoming(w)
    if change=='recipient':env['recipient']='attacker@example.test'
    elif change=='origin':env['origin_site']=str(uuid4())
    elif change=='expired':env['issued_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=70)).isoformat()
    else:env['payload']['alert_id']=str(uuid4())
    async with session(w.db) as db:
        with pytest.raises(ValueError):await sites.receive(db,peer,env)
    assert not w.mails


@pytest.mark.asyncio
async def test_replay_cannot_change_a_stored_payload(world):
    w=world;peer,env=incoming(w)
    async with session(w.db) as db:assert await sites.receive(db,peer,env)=='accepted'
    env['payload']['message']='Changed payload'
    async with session(w.db) as db:
        with pytest.raises(ValueError):await sites.receive(db,peer,env)
    assert len(w.mails)==1


@pytest.mark.asyncio
async def test_remote_provider_uncertainty_never_retries_automatically(world):
    w=world;peer,env=incoming(w);w.behaviour.error=TimeoutError('unknown provider outcome')
    async with session(w.db) as db:assert await sites.receive(db,peer,env)=='uncertain'
    w.behaviour.error=None;env['grant_token']=str(uuid4())
    async with session(w.db) as db:assert await sites.receive(db,peer,env)=='uncertain'
    assert not w.mails


@pytest.mark.asyncio
async def test_proven_provider_rejection_can_retry_with_a_fresh_grant(world):
    from app.services.alert_delivery_log import DeliveryNotSent
    w=world;peer,env=incoming(w);w.behaviour.error=DeliveryNotSent('connection refused')
    async with session(w.db) as db:assert await sites.receive(db,peer,env)=='failed'
    w.behaviour.error=None;env['grant_token']=str(uuid4())
    async with session(w.db) as db:assert await sites.receive(db,peer,env)=='accepted'
    assert len(w.mails)==1


def outgoing(w,senders):
    from app.core.crypto import encrypt_config
    import json
    pid=str(uuid4());sid=str(uuid4())
    w.db.execute("INSERT INTO notification_site_peers(id,name,site_id,direction,config) VALUES($1,'sender',$2,'outbound',$3::jsonb)",UUID(pid),UUID(sid),{'url':'https://sender.invalid','token':'secret'})
    w.db.execute('INSERT INTO notification_site_policies(channel_id,senders) VALUES($1,$2::jsonb)',UUID(w.ch.noc),[pid if v=='remote' else None for v in senders])
    return pid,sid


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_state,expected,local_count',[('unavailable','accepted',1),('accepted','accepted',0),('failed','accepted',1),('uncertain','uncertain',0),('malformed','uncertain',0)])
async def test_origin_obeys_order_and_holds_unknown_outcomes(world,monkeypatch,remote_state,expected,local_count):
    w=world;pid,sid=outgoing(w,['remote','local']);aid=w.alert()
    monkeypatch.setattr(get_settings(),'NOTIFICATION_SITE_ROUTING_ENABLED',True)
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):
            if remote_state=='unavailable':raise TimeoutError()
            return NS(status_code=200,json=lambda:{'site_id':sid})
        async def post(self,*args,**kwargs):
            if remote_state=='malformed':return NS(status_code=200,json=lambda:[])
            return NS(status_code=200,json=lambda:{'site_id':sid,'id':kwargs['json']['id'],'status':remote_state})
    monkeypatch.setattr(sites,'transport',lambda cfg:Client())
    async with session(w.db) as db:
        await has.dispatch_to_channels(db,[w.ch.noc],ctx(aid,only_recipients=['ops@example.test']))
    assert len(w.mails)==local_count
    assert w.db.scalar('SELECT state FROM alert_delivery_keys WHERE alert_id=$1',UUID(aid))==expected


@pytest.mark.parametrize('value',['http://sender','https://user:secret@sender','https://sender/path','https://sender?token=secret'])
def test_sender_origin_rejects_cleartext_and_embedded_credentials(value):
    with pytest.raises(ValueError):sites.endpoint(value)


@pytest.mark.asyncio
async def test_admin_permissions_and_machine_credential_scope(world,monkeypatch):
    import hashlib,httpx
    from fastapi import FastAPI
    from app.api.v1 import notification_sites as api
    from app.core.database import get_db
    from app.core.security import get_current_user
    w=world;peer,env=incoming(w)
    monkeypatch.setattr(get_settings(),'NOTIFICATION_SITE_ROUTING_ENABLED',True)
    token=str(peer.id)+'.'+'a'*64
    w.db.execute('UPDATE notification_site_peers SET token_hash=$1 WHERE id=$2',hashlib.sha256(token.encode()).hexdigest(),peer.id)
    w.db.execute("INSERT INTO roles(name,display_name,permissions) VALUES('site-reader','Site reader',$1::jsonb)",['settings.view'])
    user=NS(id=None,username='reader',role='site-reader',scope_tags=[])
    app=FastAPI();app.include_router(api.router);app.include_router(api.runtime)
    async def database():
        async with session(w.db) as db:
            db.answers_role_lookup=True
            yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[get_current_user]=lambda:user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture.test') as client:
        assert (await client.get('/settings/notification-sites')).status_code==200
        assert (await client.put('/settings/notification-sites/policies/'+w.ch.noc,json={'senders':[None]})).status_code==403
        assert (await client.get('/notification-site-runtime/health')).status_code==401
        headers={'Authorization':'Bearer '+token}
        assert (await client.get('/notification-site-runtime/health',headers=headers)).status_code==200
        wrong=dict(env,origin_site=str(uuid4()))
        assert (await client.post('/notification-site-runtime/deliver',headers=headers,json=wrong)).status_code==400
        response=await client.post('/notification-site-runtime/deliver',headers=headers,json=env)
        assert response.status_code==200 and response.json()['status']=='accepted'
        assert len(w.mails)==1
        w.db.execute('UPDATE notification_site_peers SET enabled=false WHERE id=$1',peer.id)
        assert (await client.get('/notification-site-runtime/health',headers=headers)).status_code==401
        assert (await client.post('/settings/notification-sites/peers/'+str(peer.id)+'/enable')).status_code==403
        w.db.execute("INSERT INTO roles(name,display_name,permissions) VALUES('site-manager','Site manager',$1::jsonb)",['settings.manage'])
        user.role='site-manager'
        assert (await client.post('/settings/notification-sites/peers/'+str(peer.id)+'/enable')).status_code==200
        assert (await client.get('/notification-site-runtime/health',headers=headers)).status_code==200
        assert (await client.put('/settings/notification-sites/policies/'+w.ch.noc,json={'senders':[str(uuid4())],'mode':'local_safety'})).status_code==400


@pytest.mark.asyncio
@pytest.mark.parametrize('late_receipt',[False,True])
async def test_local_safety_waits_and_labels_copy_unless_remote_receipt_recovers(world,monkeypatch,late_receipt):
    import httpx,time
    from app.api.v1 import alert_engine
    w=world;pid,sid=outgoing(w,['remote','local']);aid=w.alert()
    w.db.execute("UPDATE notification_site_policies SET mode='local_safety'")
    monkeypatch.setattr(get_settings(),'NOTIFICATION_SITE_ROUTING_ENABLED',True)
    monkeypatch.setattr(sites,'GRANT_SECONDS',.05);monkeypatch.setattr(sites,'SAFETY_SECONDS',.02)
    subjects=[]
    async def send(gw,to,subject,*args,**kwargs):subjects.append(subject);return True
    monkeypatch.setattr(alert_engine,'_send_email',send)
    envelope={}
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            body={'site_id':sid}
            if '/receipts/' in url:
                if not late_receipt:raise httpx.ConnectTimeout('isolated sender')
                body.update(id=envelope['id'],status='accepted')
            return NS(status_code=200,json=lambda:body)
        async def post(self,*args,**kwargs):
            envelope.update(kwargs['json']);raise httpx.ReadTimeout('response lost')
    monkeypatch.setattr(sites,'transport',lambda cfg:Client())
    start=time.monotonic()
    async with session(w.db) as db:
        await has.dispatch_to_channels(db,[w.ch.noc],ctx(aid,only_recipients=['ops@example.test']))
    assert w.db.scalar('SELECT state FROM alert_delivery_keys WHERE alert_id=$1',UUID(aid))=='accepted'
    if late_receipt:assert subjects==[]
    else:
        assert time.monotonic()-start>=.07
        assert len(subjects)==1 and '[Local safety copy]' in subjects[0]
