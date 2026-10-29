"""Admin pairing and strictly scoped machine-to-machine notification delivery."""
import hashlib
import hmac
import json
import secrets
import ssl
from typing import Literal
from uuid import UUID,uuid4

from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel,Field,ConfigDict,field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import encrypt_config,decrypt_config
from app.core.database import get_db
from app.core.security import require_permission
from app.services.audit_service import write_audit_log
from app.services import notification_sites as service

router=APIRouter(prefix='/settings/notification-sites',tags=['Notification sites'])
runtime=APIRouter(prefix='/notification-site-runtime',tags=['Notification site runtime'])
view=require_permission('settings.view','settings.manage')
manage=require_permission('settings.manage')


def commissioned():
    if not get_settings().NOTIFICATION_SITE_ROUTING_ENABLED:
        raise HTTPException(503,'Notification site routing has not been commissioned')


class Trust(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=100)
    site_id:UUID
    channel_map:dict[UUID,UUID]=Field(min_length=1,max_length=128)
    valid_days:int=Field(default=90,ge=1,le=365)


class Rotation(BaseModel):
    model_config=ConfigDict(extra="forbid")
    valid_days:int=Field(default=90,ge=1,le=365)
    overlap_seconds:int=Field(default=300,ge=0,le=3600)


class Peer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=100)
    site_id:UUID
    url:str=Field(max_length=1024)
    token:str=Field(min_length=60,max_length=200)
    ca_pem:str=Field(default='',max_length=32768)

    @field_validator('url')
    @classmethod
    def valid_url(cls,v):return service.endpoint(v)

    @field_validator('ca_pem')
    @classmethod
    def valid_ca(cls,v):
        try:ssl.create_default_context(cadata=v or None)
        except ssl.SSLError:raise ValueError('Invalid PEM certificate authority') from None
        return v


class Policy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    senders:list[UUID|None]=Field(min_length=1,max_length=8)
    mode:Literal['strict_selected','local_safety']='strict_selected'

    @field_validator('senders')
    @classmethod
    def unique(cls,v):
        if len(set(v))!=len(v):raise ValueError('Each sender can appear once')
        return v


class ChannelMap(BaseModel):
    model_config=ConfigDict(extra='forbid')
    channel_map:dict[UUID,UUID]=Field(min_length=1,max_length=128)


@router.get('')
async def config(db:AsyncSession=Depends(get_db),user=Depends(view)):
    if not get_settings().NOTIFICATION_SITE_ROUTING_ENABLED:
        return {'enabled':False,'site_id':None,'peers':[],'policies':[]}
    rows=(await db.execute(text('SELECT id,name,site_id,direction,enabled,config,channel_map,credential_expires_at,previous_valid_until,credential_revoked_at FROM notification_site_peers ORDER BY name'))).mappings().all()
    peers=[]
    for row in rows:
        data=dict(row);private=decrypt_config(data.pop('config')) if row['direction']=='outbound' else {}
        data['url']=private.get('url');data['ca_pem']=private.get('ca_pem');peers.append(data)
    policies=(await db.execute(text('SELECT channel_id,senders,mode,revision,updated_at FROM notification_site_policies'))).mappings().all()
    return {'enabled':True,'site_id':await service.identity(db),'peers':peers,'policies':policies,
            'uncertain_outcome_policy':'hold_for_reconciliation'}


async def audit(db,user,action,resource):
    await write_audit_log(db,actor=user,action=action,resource_type='notification_site',resource_id=str(resource))


@router.post('/trust',status_code=201)
async def trust(data:Trust,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    if str(data.site_id)==await service.identity(db):raise HTTPException(400,'A site cannot pair with itself')
    for cid in data.channel_map.values():
        if not (await db.execute(text('SELECT id FROM notification_channels WHERE id=:id AND enabled'),{'id':cid})).first():
            raise HTTPException(400,'Every mapped channel must exist and be enabled')
    if (await db.execute(text("SELECT id FROM notification_site_peers WHERE site_id=:id AND direction='inbound'"),{'id':data.site_id})).first():
        raise HTTPException(409,'This origin is already paired')
    pid=uuid4();token=str(pid)+'.'+secrets.token_urlsafe(48)
    await db.execute(text("INSERT INTO notification_site_peers(id,name,site_id,direction,token_hash,channel_map,credential_expires_at) VALUES(:id,:name,:site,'inbound',:hash,CAST(:mapping AS jsonb),clock_timestamp()+make_interval(days=>:days))"),
                     {'days':data.valid_days,'id':pid,'name':data.name,'site':data.site_id,'hash':hashlib.sha256(token.encode()).hexdigest(),
                      'mapping':json.dumps({str(k):str(v) for k,v in data.channel_map.items()})})
    await audit(db,user,'notification_site.trust',pid);await db.commit()
    return {'id':pid,'site_id':await service.identity(db),'token':token}


@router.post('/peers',status_code=201)
async def peer(data:Peer,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    if str(data.site_id)==await service.identity(db):raise HTTPException(400,'A site cannot pair with itself')
    if (await db.execute(text("SELECT id FROM notification_site_peers WHERE site_id=:id AND direction='outbound'"),{'id':data.site_id})).first():
        raise HTTPException(409,'This sender is already paired')
    config={'url':data.url,'token':data.token,'ca_pem':data.ca_pem}
    try:
        async with service.transport(config) as client:
            response=await client.get(data.url+'/api/v1/notification-site-runtime/health',headers={'Authorization':'Bearer '+data.token},timeout=5)
            valid=response.status_code==200 and response.json().get('site_id')==str(data.site_id)
    except Exception:valid=False
    if not valid:raise HTTPException(400,'Sender identity or authenticated TLS connection could not be verified')
    pid=uuid4()
    await db.execute(text("INSERT INTO notification_site_peers(id,name,site_id,direction,config) VALUES(:id,:name,:site,'outbound',CAST(:config AS jsonb))"),
                     {'id':pid,'name':data.name,'site':data.site_id,'config':json.dumps(encrypt_config(config))})
    await audit(db,user,'notification_site.pair',pid);await db.commit()
    return {'id':pid,'name':data.name,'site_id':data.site_id}


@router.post('/trust/{peer_id}/rotate')
async def rotate(peer_id:UUID,data:Rotation,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    token=str(peer_id)+'.'+secrets.token_urlsafe(48)
    row=(await db.execute(text("""UPDATE notification_site_peers SET
        previous_token_hash=CASE WHEN credential_revoked_at IS NULL AND
            (credential_expires_at IS NULL OR credential_expires_at>clock_timestamp())
            THEN token_hash ELSE NULL END,
        previous_valid_until=LEAST(COALESCE(credential_expires_at,'infinity'::timestamptz),
            clock_timestamp()+make_interval(secs=>:overlap)),
        token_hash=:hash,credential_expires_at=clock_timestamp()+make_interval(days=>:days),
        credential_revoked_at=NULL
        WHERE id=:id AND direction='inbound' RETURNING credential_expires_at,previous_valid_until"""),
        {'id':peer_id,'hash':hashlib.sha256(token.encode()).hexdigest(),
         'days':data.valid_days,'overlap':data.overlap_seconds})).first()
    if not row:raise HTTPException(404,'Origin not found')
    await audit(db,user,'notification_site.rotate',peer_id);await db.commit()
    return {'id':peer_id,'token':token,'expires_at':row[0],'previous_valid_until':row[1]}


@router.post('/trust/{peer_id}/revoke')
async def revoke(peer_id:UUID,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    row=(await db.execute(text("""UPDATE notification_site_peers SET enabled=false,
        token_hash=NULL,previous_token_hash=NULL,previous_valid_until=NULL,
        credential_revoked_at=clock_timestamp() WHERE id=:id AND direction='inbound' RETURNING id"""),{'id':peer_id})).first()
    if not row:raise HTTPException(404,'Origin not found')
    await audit(db,user,'notification_site.revoke',peer_id);await db.commit()
    return {'revoked':True}


@router.put('/peers/{peer_id}/connection')
async def connection(peer_id:UUID,data:Peer,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    # Keep the row and its policy references; require the same commissioned site.
    row=(await db.execute(text("SELECT site_id FROM notification_site_peers WHERE id=:id AND direction='outbound' FOR UPDATE"),{'id':peer_id})).first()
    if not row:raise HTTPException(404,'Sender not found')
    if row.site_id!=data.site_id:raise HTTPException(400,'Sender site identity cannot change')
    config={'url':data.url,'token':data.token,'ca_pem':data.ca_pem}
    try:
        async with service.transport(config) as client:
            response=await client.get(data.url+'/api/v1/notification-site-runtime/health',headers={'Authorization':'Bearer '+data.token},timeout=5)
            valid=response.status_code==200 and response.json().get('site_id')==str(data.site_id)
    except Exception:valid=False
    if not valid:raise HTTPException(400,'Sender identity or authenticated TLS connection could not be verified')
    await db.execute(text("UPDATE notification_site_peers SET name=:name,config=CAST(:config AS jsonb) WHERE id=:id"),
                     {'id':peer_id,'name':data.name,'config':json.dumps(encrypt_config(config))})
    await audit(db,user,'notification_site.connection',peer_id);await db.commit()
    return {'saved':True}


@router.put('/policies/{channel_id}')
async def policy(channel_id:UUID,data:Policy,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    if data.mode=='local_safety' and data.senders[-1] is not None:
        raise HTTPException(400,'Local safety requires this site as the final sender')
    if not (await db.execute(text('SELECT id FROM notification_channels WHERE id=:id FOR UPDATE'),{'id':channel_id})).first():
        raise HTTPException(404,'Channel not found')
    for pid in data.senders:
        if pid is not None and not (await db.execute(text("SELECT id FROM notification_site_peers WHERE id=:id AND direction='outbound' AND enabled FOR SHARE"),{'id':pid})).first():
            raise HTTPException(400,'Every remote sender must be paired and enabled')
    await db.execute(text('''INSERT INTO notification_site_policies(channel_id,senders,mode) VALUES(:id,CAST(:senders AS jsonb),:mode)
        ON CONFLICT(channel_id) DO UPDATE SET senders=EXCLUDED.senders,mode=EXCLUDED.mode,revision=notification_site_policies.revision+1,updated_at=clock_timestamp()'''),
                     {'id':channel_id,'senders':json.dumps([str(v) if v else None for v in data.senders]),'mode':data.mode})
    await audit(db,user,'notification_site.policy',channel_id);await db.commit()
    return {'saved':True}


@router.post('/peers/{peer_id}/disable')
async def disable(peer_id:UUID,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    if not (await db.execute(text('UPDATE notification_site_peers SET enabled=false WHERE id=:id RETURNING id'),{'id':peer_id})).first():
        raise HTTPException(404,'Peer not found')
    await audit(db,user,'notification_site.disable',peer_id);await db.commit()
    return {'disabled':True}


@router.post('/peers/{peer_id}/enable')
async def enable(peer_id:UUID,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    if not (await db.execute(text('UPDATE notification_site_peers SET enabled=true WHERE id=:id RETURNING id'),{'id':peer_id})).first():
        raise HTTPException(404,'Peer not found')
    await audit(db,user,'notification_site.enable',peer_id);await db.commit()
    return {'enabled':True}


@router.put('/trust/{peer_id}/channels')
async def mapping(peer_id:UUID,data:ChannelMap,db:AsyncSession=Depends(get_db),user=Depends(manage)):
    commissioned()
    for cid in data.channel_map.values():
        if not (await db.execute(text('SELECT id FROM notification_channels WHERE id=:id AND enabled'),{'id':cid})).first():
            raise HTTPException(400,'Every mapped channel must exist and be enabled')
    row=(await db.execute(text("UPDATE notification_site_peers SET channel_map=CAST(:mapping AS jsonb) WHERE id=:id AND direction='inbound' RETURNING id"),
                         {'id':peer_id,'mapping':json.dumps({str(k):str(v) for k,v in data.channel_map.items()})})).first()
    if not row:raise HTTPException(404,'Origin not found')
    await audit(db,user,'notification_site.channels',peer_id);await db.commit()
    return {'saved':True}


async def principal(request:Request,db:AsyncSession=Depends(get_db)):
    commissioned()
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer ') or len(header)>220:raise HTTPException(401,'Peer authentication required')
    token=header[7:]
    try:pid=UUID(token.split('.',1)[0])
    except ValueError:raise HTTPException(401,'Invalid peer credential') from None
    row=(await db.execute(text("""SELECT id,site_id,token_hash,channel_map,previous_token_hash,
        previous_valid_until>clock_timestamp() AS previous_live,
        credential_expires_at IS NULL OR credential_expires_at>clock_timestamp() AS current_live
        FROM notification_site_peers WHERE id=:id AND direction='inbound' AND enabled
        AND credential_revoked_at IS NULL"""),{'id':pid})).first()
    digest=hashlib.sha256(token.encode()).hexdigest()
    current=hmac.compare_digest(row.token_hash or '',digest) if row else False
    previous=hmac.compare_digest(row.previous_token_hash or '',digest) if row else False
    if not row or not ((current and row.current_live) or (previous and row.previous_live)):
        raise HTTPException(401,'Invalid peer credential')
    return row


@runtime.get('/health')
async def health(db:AsyncSession=Depends(get_db),peer=Depends(principal)):
    return {'site_id':await service.identity(db)}


@runtime.get('/receipts/{delivery_id}')
async def receipt(delivery_id:UUID,db:AsyncSession=Depends(get_db),peer=Depends(principal)):
    state=(await db.execute(text('SELECT status FROM notification_site_inbox WHERE id=:id AND origin_site=:site'),
                           {'id':delivery_id,'site':peer.site_id})).scalar()
    if state is None:raise HTTPException(404,'Receipt not found')
    return {'id':delivery_id,'site_id':await service.identity(db),'status':state}


class Envelope(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    origin_site:UUID
    alert_id:UUID
    event:str=Field(min_length=1,max_length=64)
    channel_id:UUID
    recipient:str=Field(max_length=1024)
    payload:dict
    grant_token:UUID
    issued_at:str=Field(max_length=50)
    expires_at:str=Field(max_length=50)


@runtime.post('/deliver')
async def deliver(request:Request,db:AsyncSession=Depends(get_db),peer=Depends(principal)):
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>256*1024:raise HTTPException(400,'Notification envelope exceeds the size limit')
    try:
        envelope=Envelope.model_validate_json(data).model_dump(mode='json')
        if envelope['origin_site']!=str(peer.site_id):raise ValueError('Wrong origin')
        status=await service.receive(db,peer,envelope)
    except ValueError:
        await db.rollback();raise HTTPException(400,'Invalid or unauthorized notification envelope') from None
    return {'id':envelope['id'],'site_id':await service.identity(db),'status':status}
