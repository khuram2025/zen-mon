"""Transactional NCM events with per-channel, at-least-once delivery."""
from __future__ import annotations
import json
from sqlalchemy import text


async def audit(db, actor, action, *, device_id=None, resource_id=None, metadata=None):
    await db.execute(text('''INSERT INTO ncm_audit
        (actor_id,actor_name,action,device_id,resource_id,metadata)
        VALUES (:actor,:name,:action,:device,:resource,CAST(:metadata AS jsonb))'''),
        dict(actor=getattr(actor,'id',None),name=getattr(actor,'username','scheduler'),
             action=action,device=device_id,resource=str(resource_id) if resource_id else None,
             metadata=json.dumps(metadata or {},default=str)))


async def event(db, device_id, kind, key, *, metadata=None, message=None):
    row=(await db.execute(text('''SELECT d.hostname,host(d.ip_address) AS ip,n.notify_channels
        FROM devices d JOIN device_ncm n ON n.device_id=d.id WHERE d.id=:id'''),{'id':device_id})).first()
    if not row:
        return
    summary=message or kind.replace('.',' ').capitalize()
    # Raw CLI content and exception strings never enter outbound payloads.
    payload={'event_id':key,'event_type':kind,'device_id':str(device_id),
             'hostname':row.hostname,'ip_address':row.ip,'rule_name':summary,
             'subject':f'NCM: {summary} - {row.hostname}','message':summary,
             'body':summary,'severity':'warning','status':'ACTIVE',
             'metadata':metadata or {}}
    await db.execute(text('''INSERT INTO alerts(device_id,rule_id,status,severity,message,triggered_at,metadata)
        VALUES (:id,NULL,'active','warning',:message,now(),CAST(:metadata AS jsonb))'''),
        {'id':device_id,'message':payload['subject'],'metadata':json.dumps({'ncm':True,**payload['metadata'],'event_id':key})})
    for channel in row.notify_channels or []:
        await db.execute(text('''INSERT INTO ncm_notification_outbox(device_id,event_key,channel_id,payload)
            VALUES (:id,:key,:channel,CAST(:payload AS jsonb)) ON CONFLICT(event_key,channel_id) DO NOTHING'''),
            {'id':device_id,'key':key,'channel':channel,'payload':json.dumps(payload)})


async def deliver_due(db, *, limit=50, sender=None):
    if sender is None:
        from app.services.ncm_delivery import send
        sender=send
    rows=(await db.execute(text('''SELECT id,channel_id,payload,attempts FROM ncm_notification_outbox
        WHERE status='pending' AND next_attempt_at<=now() ORDER BY created_at
        LIMIT :limit FOR UPDATE SKIP LOCKED'''),{'limit':limit})).fetchall()
    delivered=0
    for row in rows:
        channel=(await db.execute(text('SELECT enabled FROM notification_channels WHERE id=:id'),{'id':row.channel_id})).first()
        if not channel or not channel.enabled:
            await db.execute(text("UPDATE ncm_notification_outbox SET status='disabled',last_error='Channel missing or disabled' WHERE id=:id"),{'id':row.id})
            continue
        ok=False
        try:
            async with db.begin_nested():
                ok=await sender(db,[row.channel_id],row.payload)==1
        except Exception:
            # Provider messages may contain tokens/URLs. Persist only safe codes.
            pass
        attempts=row.attempts+1
        if ok:
            delivered+=1
            await db.execute(text("UPDATE ncm_notification_outbox SET status='delivered',attempts=:attempts,delivered_at=now(),last_error=NULL WHERE id=:id"),{'id':row.id,'attempts':attempts})
        else:
            await db.execute(text('''UPDATE ncm_notification_outbox SET attempts=:attempts,
                status=:status,last_error='Delivery unsuccessful',
                next_attempt_at=now()+make_interval(secs=>:delay) WHERE id=:id'''),
                {'id':row.id,'attempts':attempts,'status':'failed' if attempts>=8 else 'pending','delay':min(3600,30*2**min(attempts,7))})
    await db.commit()
    return {'processed':len(rows),'delivered':delivered}
