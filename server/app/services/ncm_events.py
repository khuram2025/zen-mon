"""Transactional NCM events with per-channel, at-least-once delivery.

Backup failures are correlated into one open incident per device: repeated
failed attempts or jobs raise its occurrence count instead of creating new
alerts, and the next successful capture closes it (resolving the alert and
sending one recovery notification). The per-attempt audit trail is kept in
ncm_backup_runs.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from sqlalchemy import text


async def audit(db, actor, action, *, device_id=None, resource_id=None, metadata=None):
    await db.execute(text('''INSERT INTO ncm_audit
        (actor_id,actor_name,action,device_id,resource_id,metadata)
        VALUES (:actor,:name,:action,:device,:resource,CAST(:metadata AS jsonb))'''),
        dict(actor=getattr(actor,'id',None),name=getattr(actor,'username','scheduler'),
             action=action,device=device_id,resource=str(resource_id) if resource_id else None,
             metadata=json.dumps(metadata or {},default=str)))


async def _device(db, device_id):
    return (await db.execute(text('''SELECT d.hostname,host(d.ip_address) AS ip,d.device_type,n.notify_channels
        FROM devices d JOIN device_ncm n ON n.device_id=d.id WHERE d.id=:id'''),{'id':device_id})).first()


# Why a backup failed, in words (ncm_errors categories). Never the raw
# exception text, which may carry CLI output or provider details.
FAILURE_LABELS = {
    'blocked': 'the device is not ready for backups', 'not_enrolled': 'the device is not enrolled',
    'credentials': 'the stored credentials could not be read', 'authentication': 'login was refused',
    'key': 'the SSH key could not be loaded', 'unreachable': 'the device could not be reached over SSH',
    'timeout': 'the device did not answer in time', 'negotiation': 'the SSH handshake failed',
    'privilege': 'the account cannot read the full configuration',
    'driver': 'no matching platform driver', 'unsupported_type': 'a capture type is not supported',
    'incomplete': 'the device output ended early', 'format': 'the output did not look like a configuration',
    'interrupted': 'the SSH session dropped', 'archive': 'the copy could not be stored',
    'cancelled': 'the job was cancelled',
}


def describe(kind, row, metadata=None, *, at=''):
    """(short summary, sentence) for an NCM event on a device.

    "Running configuration of Switch sw-01 (10.0.0.2) changed at 14:44:
    5 lines added, 2 removed, by admin."
    """
    from app.services import alert_phrasing as ap
    m = metadata or {}
    who = ap.asset_label(row.hostname, row.ip, ap.asset_type_label(getattr(row, 'device_type', None)))
    when = f' at {at}' if at else ''
    if kind == 'config.changed':
        ctype = str(m.get('config_type') or 'running').lower()
        parts = []
        if m.get('added') is not None:
            parts.append(f"{m['added']} line{'s' if m['added'] != 1 else ''} added")
        if m.get('removed') is not None:
            parts.append(f"{m['removed']} removed")
        detail = ', '.join(parts)
        if m.get('sensitive_changed'):
            detail += (', ' if detail else '') + 'including credential or key lines'
        if m.get('reported_user'):
            detail += (', ' if detail else '') + f"by {m['reported_user']}"
        sentence = f'The {ctype} configuration of {who} changed{when}' + (f': {detail}.' if detail else '.')
        return f'{ctype.capitalize()} configuration changed', sentence
    if kind == 'backup.failed':
        why = FAILURE_LABELS.get(str(m.get('failure_category') or ''), '')
        sentence = (f'Configuration backup of {who} failed{when}' + (f': {why}.' if why else '.')
                    + ' The last saved configuration is still available.')
        return 'Configuration backup failed', sentence
    if kind == 'backup.recovered':
        n = int(m.get('occurrences') or 0)
        sentence = f'Configuration backup of {who} is working again{when}' + (
            f' after {n} failed attempt{"s" if n != 1 else ""}.' if n else '.')
        return 'Configuration backup recovered', sentence
    summary = kind.replace('.', ' ').capitalize()
    return summary, f'{summary} on {who}{when}.'


def _payload(row, device_id, key, kind, summary, *, metadata=None, severity='warning', status='ACTIVE',
             sentence=None):
    # Raw CLI content and exception strings never enter outbound payloads.
    sentence = sentence or summary
    return {'event_id':key,'event_type':kind,'device_id':str(device_id),
            'hostname':row.hostname,'ip_address':row.ip,'rule_name':summary,
            'subject':f'NCM: {summary} - {row.hostname}' + (f' ({row.ip})' if row.ip and row.ip != row.hostname else ''),
            'message':sentence,'sms_body':f'ZenPlus {"resolved" if status == "RESOLVED" else severity.upper()} — {sentence}',
            'body':sentence,'severity':severity,'status':status,
            'timestamp':datetime.now(timezone.utc).isoformat(),
            'metadata':metadata or {}}


async def _clock(db):
    """Local "14:44" for now, in the organisation time zone ("" if unknown)."""
    try:
        from app.services.alert_phrasing import clock
        from app.services.regional_time import notification_branding
        return clock(datetime.now(timezone.utc), await notification_branding(db))
    except Exception:
        return ''


async def _outbox(db, device_id, row, key, payload):
    for channel in row.notify_channels or []:
        await db.execute(text('''INSERT INTO ncm_notification_outbox(device_id,event_key,channel_id,payload)
            VALUES (:id,:key,:channel,CAST(:payload AS jsonb)) ON CONFLICT(event_key,channel_id) DO NOTHING'''),
            {'id':device_id,'key':key,'channel':channel,'payload':json.dumps(payload)})


async def event(db, device_id, kind, key, *, metadata=None, message=None, severity='warning'):
    row=await _device(db,device_id)
    if not row:
        return None
    default_summary, sentence = describe(kind, row, metadata, at=await _clock(db))
    summary=message or default_summary
    payload=_payload(row,device_id,key,kind,summary,metadata=metadata,severity=severity,sentence=sentence)
    alert=(await db.execute(text('''INSERT INTO alerts(device_id,rule_id,status,severity,message,triggered_at,metadata)
        VALUES (:id,NULL,'active',:severity,:message,now(),CAST(:metadata AS jsonb)) RETURNING id'''),
        {'id':device_id,'severity':severity,'message':payload['subject'],
         'metadata':json.dumps({'ncm':True,'ncm_event':kind,**payload['metadata'],'event_id':key})})).scalar()
    await _outbox(db,device_id,row,key,payload)
    return alert


async def open_incident(db, device_id, *, category, message, job_id=None):
    """Record a final backup failure. Returns (incident_id, created)."""
    existing=(await db.execute(text('''SELECT id,occurrences,alert_id FROM ncm_incidents
        WHERE device_id=:d AND kind='backup_failure' AND status='open' FOR UPDATE'''),{'d':device_id})).first()
    if existing:
        await db.execute(text('''UPDATE ncm_incidents SET occurrences=occurrences+1,last_seen_at=now(),
            failure_category=:cat,last_message=:msg,last_job_id=:job WHERE id=:id'''),
            {'id':existing.id,'cat':category,'msg':message,'job':job_id})
        if existing.alert_id:
            await db.execute(text('''UPDATE alerts SET metadata=COALESCE(metadata,'{}'::jsonb)||CAST(:meta AS jsonb)
                WHERE id=:id AND status<>'resolved' '''),
                {'id':existing.alert_id,'meta':json.dumps({'occurrences':existing.occurrences+1,
                 'failure_category':category,'last_message':message})})
        return existing.id, False
    incident=(await db.execute(text('''INSERT INTO ncm_incidents(device_id,failure_category,last_message,last_job_id)
        VALUES(:d,:cat,:msg,:job) RETURNING id'''),{'d':device_id,'cat':category,'msg':message,'job':job_id})).scalar()
    alert=await event(db,device_id,'backup.failed',f'incident:{incident}:open',
                      metadata={'incident_id':str(incident),'failure_category':category,'occurrences':1,
                                'last_message':message},
                      message='Configuration backup failed')
    await db.execute(text('UPDATE ncm_incidents SET alert_id=:a WHERE id=:id'),{'a':alert,'id':incident})
    return incident, True


async def resolve_incident(db, device_id, *, run_id=None):
    """Close the open backup-failure incident after a successful capture."""
    row=(await db.execute(text('''UPDATE ncm_incidents SET status='recovered',recovered_at=now(),recovered_run_id=:run
        WHERE device_id=:d AND kind='backup_failure' AND status='open' RETURNING id,alert_id,occurrences'''),
        {'d':device_id,'run':run_id})).first()
    # Legacy per-attempt alerts (before incidents existed) close with recovery too.
    await db.execute(text('''UPDATE alerts SET status='resolved',resolved_at=now()
        WHERE device_id=:d AND status IN ('active','acknowledged')
          AND metadata->>'ncm'='true'
          AND (metadata->>'ncm_event'='backup.failed' OR message LIKE 'NCM: Configuration backup failed%')'''),
        {'d':device_id})
    if not row:
        return None
    device=await _device(db,device_id)
    if device:
        key=f'incident:{row.id}:recovered'
        meta={'incident_id':str(row.id),'occurrences':row.occurrences}
        summary, sentence = describe('backup.recovered', device, meta, at=await _clock(db))
        payload=_payload(device,device_id,key,'backup.recovered',summary,
                         metadata=meta,severity='info',status='RESOLVED',sentence=sentence)
        await _outbox(db,device_id,device,key,payload)
    return row.id


async def deliver_due(db, *, limit=50, sender=None):
    if sender is None:
        from app.services.ncm_delivery import send
        sender=send
    rows=(await db.execute(text('''SELECT id,channel_id,payload,attempts,created_at FROM ncm_notification_outbox
        WHERE status='pending' AND next_attempt_at<=now() ORDER BY created_at
        LIMIT :limit FOR UPDATE SKIP LOCKED'''),{'limit':limit})).fetchall()
    delivered=0
    for row in rows:
        channel=(await db.execute(text('SELECT enabled FROM notification_channels WHERE id=:id'),{'id':row.channel_id})).first()
        if not channel or not channel.enabled:
            await db.execute(text("UPDATE ncm_notification_outbox SET status='disabled',last_error='Channel missing or disabled' WHERE id=:id"),{'id':row.id})
            continue
        ok=False
        error='Delivery unsuccessful'
        terminal=False
        try:
            async with db.begin_nested():
                payload = dict(row.payload)
                if not payload.get('timestamp') and getattr(row, 'created_at', None):
                    payload['timestamp'] = row.created_at.isoformat()
                ok=await sender(db,[row.channel_id],payload)==1
        except Exception as exc:
            # Provider messages may contain tokens/URLs. Persist only safe codes.
            from app.services.ncm_delivery import safe_error
            error=safe_error(exc)
            from app.services.sms_transport import SMSBatchError
            if isinstance(exc, SMSBatchError):
                # Preserve successful/possibly successful recipients before
                # the next outbox attempt; only definite failures may retry.
                payload={**row.payload, '_sms_delivery_outcomes': {
                    o['recipient']: o['outcome'] for o in exc.outcomes
                    if o['outcome'] in ('accepted', 'uncertain')}}
                await db.execute(text('UPDATE ncm_notification_outbox SET payload=CAST(:payload AS jsonb) WHERE id=:id'),
                                 {'id':row.id,'payload':json.dumps(payload)})
                terminal=not any(o['outcome']=='failed' for o in exc.outcomes)
        attempts=row.attempts+1
        if ok:
            delivered+=1
            await db.execute(text("UPDATE ncm_notification_outbox SET status='delivered',attempts=:attempts,delivered_at=now(),last_error=NULL WHERE id=:id"),{'id':row.id,'attempts':attempts})
        else:
            await db.execute(text('''UPDATE ncm_notification_outbox SET attempts=:attempts,
                status=:status,last_error=:error,
                next_attempt_at=now()+make_interval(secs=>:delay) WHERE id=:id'''),
                {'id':row.id,'attempts':attempts,'error':error,'status':'failed' if terminal or attempts>=8 else 'pending','delay':min(3600,30*2**min(attempts,7))})
    await db.commit()
    return {'processed':len(rows),'delivered':delivered}
