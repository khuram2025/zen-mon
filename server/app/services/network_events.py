"""Syslog decoding, persistence and event-rule dispatch (offline-testable)."""
from datetime import datetime, timezone
import ipaddress
import json
import re
from uuid import uuid4
from sqlalchemy import text
from app.services import notification_context


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
    from app.services.device_groups.membership import groups_of, in_group
    syslog_groups = await groups_of(db, "device", did) if did and rules else None
    if did and rules:
        # Shared rules disabled for this device only (migrate-187).
        from app.services import alert_rule_exclusions as rx
        rules = rx.drop_device(rules, await rx.load(db), did)
    from app.services import alert_funnel as funnel
    priority_ok = await funnel.priority_supported(db) if rules else False
    tz = await get_configured_timezone(db)
    from app.services import event_alerts
    created = updated = 0
    for r in rules:
        if r['device_id'] and str(r['device_id']) != did:
            continue
        if r['group_id'] and (not device or not in_group(r['group_id'], syslog_groups, device['group_id'])):
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
        if silenced:
            continue
        cooldown = max(1, r['cooldown'] or 0)
        # One open row per condition (rule + device; the source address for an
        # unknown device): a repeat updates it instead of stacking another row.
        open_row = (await db.execute(text(f'''SELECT id, triggered_at, metadata FROM alerts
            WHERE rule_id = :rid AND status IN {event_alerts.OPEN_STATUSES_SQL}
              AND metadata->>'syslog_source' IS NOT NULL
              AND device_id IS NOT DISTINCT FROM CAST(:did AS uuid)
              AND (CAST(:did AS uuid) IS NOT NULL OR metadata->>'syslog_source' = :source)
            ORDER BY triggered_at DESC LIMIT 1 FOR UPDATE'''),
            {'rid': r['id'], 'did': did, 'source': event['source_ip']})).mappings().first()
        now = datetime.now(timezone.utc)
        repeat_patch = None
        if open_row:
            repeat_patch = event_alerts.occurrence_patch(
                open_row['metadata'], now, event['message'], event_id=event['id'],
                extra={'event_id': event['id'], 'last_syslog_source': event['source_ip'],
                       'last_syslog_severity': event['severity']})
            await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb)"
                                  " || CAST(:m AS jsonb) WHERE id = :id"),
                             {'id': open_row['id'], 'm': json.dumps(repeat_patch)})
            updated += 1
            await db.commit()
            if not event_alerts.repeat_page_due(open_row['metadata'], open_row['triggered_at'], now, cooldown):
                continue
            alert = (open_row['id'],)
        else:
            prior = (await db.execute(text('''SELECT id FROM alerts WHERE rule_id = :rid
                AND metadata->>'syslog_source' = :source
                AND triggered_at > now() - make_interval(secs => :cooldown) LIMIT 1'''),
                {'rid': r['id'], 'source': event['source_ip'], 'cooldown': cooldown})).first()
            if prior:
                continue
            meta = {'syslog_source': event['source_ip'], 'event_id': event['id'], 'notified': False,
                    **event_alerts.first_metadata(now, event['message'], event['id'])}
            # The incident's priority is fixed when it is raised (migrate-164).
            p_col, p_val, p_params = funnel.priority_insert(priority_ok, r.get('priority'), r['severity'])
            alert = (await db.execute(text(f'''INSERT INTO alerts
                (device_id, rule_id, status, severity, message, triggered_at, metadata{p_col})
                VALUES (:did, :rid, 'active', :severity, :message, now(), CAST(:meta AS jsonb){p_val}) RETURNING id'''),
                {'did': did, 'rid': r['id'], 'severity': r['severity'], 'message': event['message'],
                 'meta': json.dumps(meta), **p_params})).first()
            created += 1
            await db.commit()
        if notifications_allowed(r['schedule_start'], r['schedule_end'], r['schedule_days'], tz):
            hostname = device['hostname'] if device else event['source_ip']
            from app.services import alert_phrasing as ap
            values = {'rule_name': r['name'], 'hostname': hostname, 'ip_address': event['source_ip'],
                      'event_message': event['message'], 'trap_message': event['message'],
                      'timestamp': event['received_at'].strftime('%H:%M UTC on %d %b %Y'),
                      'syslog_severity': event['severity'], 'severity': str(r['severity'] or 'warning').upper(),
                      'status': 'SYSLOG',
                      'event_sentence': f'{hostname} sent a matching syslog event.',
                      # {site}, {group}, {site_suffix} of the sending device.
                      **(await notification_context.asset_place(db, {'device_id': did})),
                      # Severity word and a capped message for the SMS.
                      **ap.syslog_variables(event['severity'], event['message'])}
            def render(field):
                template = ap.effective_template(r.get(field), ap.default_templates('syslog')[field])
                return re.sub(r'\{([a-z_]+)\}', lambda m: str(values.get(m[1], m[0])), template)
            sent = await dispatch(db, r['notify_channels'] or [], {
                'subject': render('email_subject'), 'message': render('sms_template'), 'body': render('email_body'),
                # The chat/incident title: which device, what it said.
                'event_sentence': (f"{hostname} sent a matching syslog event "
                                   f"({values['syslog_severity_label']}): {values['event_brief']}"),
                'hostname': hostname, 'status': 'SYSLOG', 'severity': r['severity'], 'rule_name': r['name'],
                'rule_id': str(r['id']), 'triggered_at': event['received_at'].isoformat(),
                # Keyed on the event's alert row (one page per incident, channel
                # and recipient) and linked to it in the delivery log.
                'alert_id': str(alert[0]),
                'delivery_event': event_alerts.repeat_event(repeat_patch) if repeat_patch else 'trigger',
                **funnel.notification_fields(r['severity'], r.get('priority'), trigger=True)})
            if repeat_patch is None:
                from app.services.alert_notify_state import stamp
                await stamp(db, alert[0], bool(sent))
            elif sent:
                await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb)"
                                      " || CAST(:m AS jsonb) WHERE id = :id"),
                                 {'id': alert[0], 'm': json.dumps({'notified': True,
                                                                   'last_notified_at': now.isoformat()})})
    await db.commit()
    return {'replayed': False, 'alerts_created': created, 'alerts_updated': updated}
