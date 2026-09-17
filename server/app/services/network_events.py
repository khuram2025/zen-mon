"""Syslog decoding, persistence and event-rule dispatch (offline-testable)."""
from datetime import datetime, timezone
import ipaddress
import json
import re
from uuid import uuid4
from sqlalchemy import text


def parse_syslog(data: bytes, source_ip: str):
    if not data or len(data) > 65507:
        raise ValueError('Syslog datagram must contain 1..65507 bytes')
    raw = data.decode('utf-8', 'replace').replace('\x00', '')
    match = re.match(r'^<(\d{1,3})>(.*)$', raw, re.S)
    if not match or int(match[1]) > 191:
        raise ValueError('Invalid syslog priority')
    priority, body = int(match[1]), match[2]
    hostname = application = ''
    # RFC 5424 header; preserve structured data with the message to avoid
    # treating escaped brackets as field delimiters. Timestamp is untrusted.
    modern = re.match(r'^1 (\S+) (\S+) (\S+) (\S+) (\S+) (.*)$', body, re.S)
    if modern:
        hostname, application, message = modern[2], modern[3], modern[6]
    else:
        legacy = re.match(r'^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} (\S+) (.*)$', body, re.S)
        hostname, message = (legacy[1], legacy[2]) if legacy else ('', body)
    return dict(id=str(uuid4()), source_ip=str(ipaddress.ip_address(source_ip)),
                facility=priority // 8, severity=priority % 8,
                reported_hostname=hostname, application=application, message=message,
                received_at=datetime.now(timezone.utc))


async def ingest_syslog(db, event, dispatch=None):
    from app.api.v1.alert_engine import _find_suppressing_dependency, _device_in_maintenance
    from app.services.host_alert_service import dispatch_to_channels
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    from app.services.tag_service import tag_set
    from app.services.network_conditions import OPS
    dispatch = dispatch or dispatch_to_channels
    # Only the socket source identifies the device; never trust the hostname
    # inside an unauthenticated datagram for scope or identity.
    device = (await db.execute(text('SELECT id, hostname, group_id, tags, status, device_type, location '
                                    'FROM devices WHERE ip_address = CAST(:ip AS inet) LIMIT 1'),
                               {'ip': event['source_ip']})).mappings().first()
    did = str(device['id']) if device else None
    inserted = (await db.execute(text('''INSERT INTO network_events
        (id, received_at, source_ip, device_id, facility, severity, reported_hostname, application, message)
        VALUES (:id, :received_at, CAST(:source_ip AS inet), :device_id, :facility, :severity,
                :reported_hostname, :application, :message)
        ON CONFLICT (id) DO NOTHING RETURNING id'''), {**event, 'device_id': did})).first()
    if not inserted:
        return {'replayed': True, 'alerts_created': 0}
    suppressed = device and (device['status'] == 'maintenance' or await _device_in_maintenance(db, did)
                             or await _find_suppressing_dependency(db, did))
    if suppressed:
        await db.execute(text("UPDATE network_events SET metadata = jsonb_build_object('suppressed', true) WHERE id = :id"), {'id': event['id']})
        await db.commit()
        return {'suppressed': True, 'alerts_created': 0}
    rules = (await db.execute(text("SELECT * FROM alert_rules WHERE enabled = true AND metric = 'syslog'"))).mappings().all()
    tz = await get_configured_timezone(db)
    created = 0
    for r in rules:
        if r['device_id'] and str(r['device_id']) != did:
            continue
        if r['group_id'] and (not device or r['group_id'] != device['group_id']):
            continue
        if r['scope_tag'] and (not device or r['scope_tag'].lower() not in tag_set(device['tags'])):
            continue
        if r['device_type'] and (not device or r['device_type'] != device['device_type']):
            continue
        if r['location'] and (not device or r['location'].lower() not in (device['location'] or '').lower()):
            continue
        if r['target'] and r['target'].lower() not in event['message'].lower():
            continue
        if r['operator'] not in OPS or not OPS[r['operator']](event['severity'], float(r['threshold'])):
            continue
        lock = f"syslog:{r['id']}:{did or event['source_ip']}"
        await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': lock})
        silenced = (await db.execute(text('''SELECT 1 FROM alert_silences WHERE device_id = :did
            AND dedupe = :dedupe AND (until IS NULL OR until > now()) LIMIT 1'''),
            {'did': did, 'dedupe': f"rule:{r['id']}"})).first() if did else None
        prior = (await db.execute(text('''SELECT id FROM alerts WHERE rule_id = :rid
            AND metadata->>'syslog_source' = :source
            AND triggered_at > now() - make_interval(secs => :cooldown) LIMIT 1'''),
            {'rid': r['id'], 'source': event['source_ip'], 'cooldown': max(1, r['cooldown'] or 0)})).first()
        if silenced or prior:
            continue
        meta = {'syslog_source': event['source_ip'], 'event_id': event['id'], 'notified': False}
        alert = (await db.execute(text('''INSERT INTO alerts
            (device_id, rule_id, status, severity, message, triggered_at, metadata)
            VALUES (:did, :rid, 'active', :severity, :message, now(), CAST(:meta AS jsonb)) RETURNING id'''),
            {'did': did, 'rid': r['id'], 'severity': r['severity'], 'message': event['message'], 'meta': json.dumps(meta)})).first()
        created += 1
        await db.commit()
        if notifications_allowed(r['schedule_start'], r['schedule_end'], r['schedule_days'], tz):
            hostname = device['hostname'] if device else event['source_ip']
            from app.services import alert_phrasing as ap
            values = {'rule_name': r['name'], 'hostname': hostname, 'ip_address': event['source_ip'],
                      'event_message': event['message'], 'trap_message': event['message'],
                      'syslog_severity': event['severity'], 'severity': r['severity'], 'status': 'SYSLOG',
                      'event_sentence': f'{hostname} sent a matching syslog event.'}
            def render(field):
                template = r.get(field) or ap.default_templates('syslog')[field]
                return re.sub(r'\{([a-z_]+)\}', lambda m: str(values.get(m[1], m[0])), template)
            sent = await dispatch(db, r['notify_channels'] or [], {
                'subject': render('email_subject'), 'message': render('sms_template'), 'body': render('email_body'),
                'hostname': hostname, 'status': 'SYSLOG', 'severity': r['severity'], 'rule_name': r['name'],
                'rule_id': str(r['id']), 'triggered_at': event['received_at'].isoformat()})
            from app.services.alert_notify_state import stamp
            await stamp(db, alert[0], bool(sent))
    await db.commit()
    return {'replayed': False, 'alerts_created': created}
