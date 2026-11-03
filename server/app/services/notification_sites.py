"""Conservative cross-site sender selection with durable remote receipts.

Only a definitely-unsent attempt may advance to another sender. An unknown
remote outcome remains uncertain for operator reconciliation; it is never
reported as provider acceptance or automatically duplicated at another site.
"""
from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import json
import ssl
import time
from uuid import UUID, uuid4, uuid5
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text

from app.core.crypto import decrypt_config
from app.services import alert_delivery_log as dl

GRANT_SECONDS = 60
SAFETY_SECONDS = 10
MAX_CLOCK_SKEW = 5


class OutcomeUncertain(Exception):
    """A sender might already have handed the message to its provider."""


def endpoint(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')):
        raise ValueError('A sender requires an HTTPS origin with no credentials or path')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid sender port')
    return value.rstrip('/')


def transport(config):
    return httpx.AsyncClient(verify=ssl.create_default_context(cadata=config.get('ca_pem') or None),
                            trust_env=False, follow_redirects=False, timeout=20)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


async def identity(db):
    return str((await db.execute(text('SELECT id FROM notification_site_identity WHERE singleton'))).scalar_one())


async def attempt(db, ctx, channel, recipient, sender, outcome):
    await db.execute(text('''INSERT INTO notification_site_attempts
        (alert_id,event,channel_id,recipient,sender_id,outcome)
        VALUES(CAST(:aid AS uuid),:event,CAST(:channel AS uuid),:recipient,CAST(:sender AS uuid),:outcome)'''),
        {'aid':str(ctx['alert_id']), 'event':ctx['delivery_event'], 'channel':channel,
         'recipient':recipient, 'sender':sender, 'outcome':outcome})
    await db.commit()


async def route(db, channel, recipient, ctx, local_send, guard=None):
    """Use an admin's ordered senders; NULL means this incident's local site.

The origin's delivery-key claim is already durable when this is called. Remote
receipts have a stable identity across origin-worker crashes and claim retries.
"""
    policy=(await db.execute(text('SELECT senders,mode FROM notification_site_policies WHERE channel_id=CAST(:id AS uuid)'),
                             {'id':channel})).first()
    if policy is None:
        return await local_send()
    if not ctx.get('alert_id') or not ctx.get('delivery_event'):
        raise dl.DeliveryNotSent('Site routing requires a durable incident delivery identity')
    origin=await identity(db)
    safety_copy=False
    for sender in policy.senders:
        if safety_copy and sender is not None:continue
        if guard:await guard()
        if sender is None:
            try:
                ok=await local_send(safety=True) if safety_copy else await local_send()
            except Exception as exc:
                state=dl.outcome_for_exception(exc)
                await attempt(db,ctx,channel,recipient,None,state)
                if state=='uncertain':raise OutcomeUncertain('Local provider outcome requires reconciliation') from None
                continue
            await attempt(db,ctx,channel,recipient,None,'accepted' if ok else 'failed')
            if ok:return True
            continue
        peer=(await db.execute(text("SELECT site_id,config FROM notification_site_peers WHERE id=CAST(:id AS uuid) AND direction='outbound' AND enabled"),
                               {'id':sender})).first()
        if peer is None:
            await attempt(db,ctx,channel,recipient,sender,'unavailable');continue
        config=decrypt_config(peer.config)
        url=endpoint(config['url'])+'/api/v1/notification-site-runtime'
        headers={'Authorization':'Bearer '+config['token']}
        payload={k:v for k,v in ctx.items() if not k.startswith('_') and k not in ('recipients','only_recipients')}
        envelope={'origin_site':origin,'alert_id':str(ctx['alert_id']),'event':ctx['delivery_event'],
                  'channel_id':channel,'recipient':recipient,'payload':payload}
        envelope['id']=str(uuid5(UUID(origin),digest([str(ctx['alert_id']),ctx['delivery_event'],channel,recipient])))
        try:
            async with transport(config) as client:
                # Health probes carry no notification and cannot authorize a send.
                try:
                    health=await client.get(url+'/health',headers=headers,timeout=3)
                    healthy=health.status_code==200 and health.json().get('site_id')==str(peer.site_id)
                except Exception:healthy=False
                if not healthy:
                    await attempt(db,ctx,channel,recipient,sender,'unavailable');continue
                if guard:await guard()
                now=dt.datetime.now(dt.timezone.utc)
                grant_started=time.monotonic()
                envelope.update(grant_token=str(uuid4()),issued_at=now.isoformat(),
                                expires_at=(now+dt.timedelta(seconds=GRANT_SECONDS)).isoformat())
                try:
                    result=await client.post(url+'/deliver',headers=headers,json=envelope)
                except (httpx.ConnectError,httpx.ConnectTimeout):
                    # No HTTP request could have reached the sender.
                    await attempt(db,ctx,channel,recipient,sender,'unavailable');continue
                except Exception:
                    raise OutcomeUncertain('Remote sender response was not confirmed') from None
                if result.status_code in (400,401,403,404,409,422):
                    # These runtime responses are emitted before dispatch.
                    await attempt(db,ctx,channel,recipient,sender,'failed');continue
                if result.status_code!=200:
                    raise OutcomeUncertain('Remote sender response was not confirmed')
                try:body=result.json()
                except Exception:raise OutcomeUncertain('Remote receipt was malformed') from None
                if not isinstance(body,dict):raise OutcomeUncertain('Remote receipt was malformed')
                if body.get('id')!=envelope['id'] or body.get('site_id')!=str(peer.site_id):
                    raise OutcomeUncertain('Remote receipt identity mismatch')
                state=body.get('status')
                if state not in ('accepted','failed','uncertain'):
                    raise OutcomeUncertain('Remote receipt did not report a terminal outcome')
                if state=='uncertain':raise OutcomeUncertain('Remote provider outcome requires reconciliation')
                await attempt(db,ctx,channel,recipient,sender,state)
                if state=='accepted':return True
        except OutcomeUncertain:
            await attempt(db,ctx,channel,recipient,sender,'uncertain')
            if policy.mode!='local_safety' or not policy.senders or policy.senders[-1] is not None:
                raise
            # An explicit availability-first policy permits a visibly labelled
            # local copy after the old grant and margin. This is NOT exactly
            # once: a paused sender or unobservable provider acceptance may
            # produce a duplicate. The origin remains the incident authority.
            deadline=grant_started+GRANT_SECONDS+SAFETY_SECONDS
            while time.monotonic()<deadline:
                if guard:await guard()
                accepted=False
                try:
                    async with transport(config) as check:
                        result=await check.get(url+'/receipts/'+envelope['id'],headers=headers,timeout=3)
                        body=result.json() if result.status_code==200 else {}
                        if (body.get('id')==envelope['id'] and body.get('site_id')==str(peer.site_id)
                                and body.get('status')=='accepted'):
                            accepted=True
                except Exception:pass
                if accepted:
                    await attempt(db,ctx,channel,recipient,sender,'accepted');return True
                await asyncio.sleep(max(0,min(5,deadline-time.monotonic())))
            safety_copy=True
    raise dl.DeliveryNotSent('No configured notification site accepted this delivery')


class InboxLedger:
    """Receipt storage for a foreign incident; no local incident is fabricated."""
    def __init__(self, envelope, token):
        self.envelope,self.token=envelope,token

    def __getattr__(self,name):return getattr(dl,name)

    async def delivered_recipients(self,*args,**kwargs):return {}

    async def claim_key(self,db,alert_id,event,channel_id,recipient='',**kwargs):
        row=(await db.execute(text("SELECT id FROM notification_site_inbox WHERE id=CAST(:id AS uuid) AND claim_token=CAST(:token AS uuid) AND status='processing' AND grant_expires_at>clock_timestamp()"),
                              {'id':self.envelope['id'],'token':self.token})).first()
        if not row:raise dl.DeliveryNotSent('Remote delivery grant is no longer current')
        return self.token,None

    async def finish_keys(self,db,tokens,state,error=None,**kwargs):
        if tokens != [self.token] or state not in ('accepted','failed','uncertain'):
            raise ValueError('Invalid remote receipt')
        await db.execute(text("UPDATE notification_site_inbox SET status=:state,finished_at=clock_timestamp() WHERE id=CAST(:id AS uuid) AND claim_token=CAST(:token AS uuid) AND status='processing'"),
                         {'id':self.envelope['id'],'token':self.token,'state':state})

    async def record(self,*args,**kwargs):
        # The inbox is the foreign delivery audit record; local alert FKs do not apply.
        return None


async def receive(db,peer,envelope):
    from app.services import host_alert_service as has
    from app.core.crypto import decrypt_config
    now=dt.datetime.now(dt.timezone.utc)
    if envelope['origin_site']!=str(peer.site_id):raise ValueError('Wrong origin')
    issued=dt.datetime.fromisoformat(envelope['issued_at'])
    expires=dt.datetime.fromisoformat(envelope['expires_at'])
    if issued.tzinfo is None or expires.tzinfo is None:
        raise ValueError('Timezone required')
    if (abs((now-issued).total_seconds())>MAX_CLOCK_SKEW
            or not 0<(expires-issued).total_seconds()<=GRANT_SECONDS
            or (expires-now).total_seconds()<=SAFETY_SECONDS+20):
        raise ValueError('Remote grant is expired, skewed or too short')
    # Process-local monotonic admission never extends an old grant after a clock adjustment.
    deadline=time.monotonic()+min(GRANT_SECONDS,(expires-now).total_seconds())-SAFETY_SECONDS
    mapping=peer.channel_map.get(envelope['channel_id'])
    if not mapping:raise ValueError('Channel is not delegated to this peer')
    channel=(await db.execute(text('SELECT type,config,enabled FROM notification_channels WHERE id=CAST(:id AS uuid)'),{'id':mapping})).first()
    if not channel or not channel.enabled:raise ValueError('Mapped channel is unavailable')
    cfg=decrypt_config(channel.config)
    recipients=has._recipients(cfg.get('recipients' if channel.type=='email' else 'phone_numbers',''),channel.type) if channel.type in ('email','sms') else [('', '')]
    if envelope['recipient'] not in {k for k,_ in recipients}:raise ValueError('Recipient is outside delegated channel scope')
    payload=envelope['payload']
    if not all(isinstance(payload.get(k),str) and payload[k] for k in ('subject','message')):
        raise ValueError('Notification text is required')
    if payload.get('alert_id')!=envelope['alert_id'] or payload.get('delivery_event')!=envelope['event']:
        raise ValueError('Incident identity mismatch')
    expected=str(uuid5(UUID(str(peer.site_id)),digest([envelope['alert_id'],envelope['event'],envelope['channel_id'],envelope['recipient']])))
    if envelope['id']!=expected:raise ValueError('Delivery identity mismatch')
    immutable={k:envelope[k] for k in ('origin_site','alert_id','event','channel_id','recipient','payload')}
    fingerprint=digest(immutable)
    params={**envelope,'origin_site':str(peer.site_id),'hash':fingerprint,'token':envelope['grant_token'],'expires_at':expires}
    inserted=(await db.execute(text('''INSERT INTO notification_site_inbox
        (id,origin_site,alert_id,event,channel_id,recipient,payload_hash,status,claim_token,grant_expires_at)
        VALUES(CAST(:id AS uuid),CAST(:origin_site AS uuid),CAST(:alert_id AS uuid),:event,CAST(:channel_id AS uuid),
        :recipient,:hash,'processing',CAST(:token AS uuid),CAST(:expires_at AS timestamptz)) ON CONFLICT DO NOTHING RETURNING id'''),params)).first()
    row=(await db.execute(text('SELECT * FROM notification_site_inbox WHERE id=CAST(:id AS uuid) FOR UPDATE'),params)).first()
    if row.payload_hash!=fingerprint:raise ValueError('Delivery payload changed')
    if not inserted and row.status=='failed' and str(row.claim_token)!=envelope['grant_token']:
        await db.execute(text("UPDATE notification_site_inbox SET status='processing',claim_token=CAST(:token AS uuid),grant_expires_at=CAST(:expires_at AS timestamptz),finished_at=NULL WHERE id=CAST(:id AS uuid)"),params)
    elif not inserted:
        # A crash or disconnected HTTP caller cannot authorize a second send.
        if row.status=='processing':
            await db.commit();return 'uncertain'
        await db.commit();return row.status
    await db.commit()
    ledger=InboxLedger(envelope,envelope['grant_token'])
    ctx={k:v for k,v in payload.items() if not k.startswith('_') and k not in ('recipients','only_recipients')}
    ctx.update(_require_delivery_key=True,_skip_grouping=True,only_recipients=[envelope['recipient']],
               _outbox_route_targets={mapping:[envelope['recipient']]})
    async def guard():
        if time.monotonic()+20>=deadline:raise dl.DeliveryNotSent('Remote grant expired before provider dispatch')
        current=(await db.execute(text('SELECT enabled,channel_map FROM notification_site_peers WHERE id=:id'),{'id':peer.id})).first()
        if not current or not current.enabled or current.channel_map.get(envelope['channel_id'])!=mapping:
            raise dl.DeliveryNotSent('Peer authorization revoked')
    try:
        await has.dispatch_to_channels(db,[mapping],ctx,delivery_guard=guard,delivery_ledger=ledger,site_routing=False)
    except Exception:
        await db.rollback()
    state=(await db.execute(text('SELECT status FROM notification_site_inbox WHERE id=CAST(:id AS uuid)'),params)).scalar_one()
    if state=='processing':
        await ledger.finish_keys(db,[envelope['grant_token']],'uncertain');await db.commit();return 'uncertain'
    return state
