"""Alert Rules for confirmed synthetic failures and multi-window SLO burns.

Producers supply a confirmed result, never an individual retry or missing data.
Each rule/entity pair owns one incident. Local APM notification settings are a
fallback when no enabled central rule matches that entity.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text

from app.services import alert_funnel, alert_notify_state as ns, alert_phrasing as ap
from app.services.host_alert_service import dispatch_to_channels

SYNTHETIC = 'apm_synthetic_down'
SLO = 'apm_slo_burn'
EVENT_METRICS = {SYNTHETIC, SLO}


def validate_definition(rule):
    conditions = rule.get('conditions') or []
    metric = rule.get('metric')
    if metric not in EVENT_METRICS and not any(c.get('metric') in EVENT_METRICS for c in conditions):
        return
    if metric not in EVENT_METRICS or len(conditions) > 1:
        raise ValueError('APM event triggers require a single condition')
    condition = conditions[0] if conditions else rule
    if condition.get('metric') != metric or condition.get('operator') not in ('==', 'eq') or condition.get('threshold') != 1:
        raise ValueError('APM event triggers use == 1; confirmation is configured on the scenario or SLO')
    if any(rule.get(k) for k in ('device_id', 'group_id', 'device_type', 'location', 'scope_tag',
                                'service_check_id', 'service_check_group_id', 'server_id', 'trap_oid')):
        raise ValueError('APM event triggers use an All scope or a specific scenario/SLO')
    if rule.get('min_duration') or rule.get('clear_duration'):
        raise ValueError('APM event confirmation is configured on the scenario or SLO; extra holds must be zero')
    if rule.get('trigger_on', 'any') not in ('any', 'down'):
        raise ValueError('APM event rules trigger on failure; enable recovery notifications for recovery')
    if rule.get('target'):
        try:
            UUID(str(rule['target']))
        except (ValueError, TypeError):
            raise ValueError('Select a scenario or SLO by its ID, or use the All scope') from None


def phrasing(metric, name, details, *, recovery=False, duration=''):
    if metric == SYNTHETIC:
        sentence = (f'Synthetic scenario “{name}” is passing again.' if recovery else
                    f'Synthetic scenario “{name}” is down after its configured retries. '
                    f'Failed step: “{details.get("failed_step") or "Unknown step"}”. '
                    f'Error: {details.get("error") or "Scenario failed"}.')
        condition = 'Synthetic scenario failed after retries'
    else:
        sentence = (f'SLO “{name}” is no longer breaching its burn thresholds.' if recovery else
                    f'SLO “{name}” for service “{details.get("service") or "Unknown service"}” '
                    f'is burning its error budget: {details.get("long_burn")}× / '
                    f'{details.get("short_burn")}× in the {details.get("tier", "configured")} tier '
                    f'(threshold {details.get("factor")}× in both windows).')
        condition = 'SLO multi-window burn threshold exceeded'
    if recovery and duration:
        sentence += f' The condition was active for {duration}.'
    return {**details, 'scenario_name': name if metric == SYNTHETIC else '',
            'slo_name': name if metric == SLO else '', 'hostname': name, 'host_suffix': '',
            'event_sentence': sentence, 'condition_sentence': condition + '.',
            'condition_label': condition, 'condition': condition,
            'reading': 'Passing' if recovery else 'Confirmed failure',
            'duration': duration, 'duration_sentence': f'Active for {duration}.' if duration else '',
            'duration_suffix': f' Active for {duration}.' if duration else '',
            'status': 'RESOLVED' if recovery else 'DOWN', 'ip_address': ''}


def _render(template, variables):
    for key, value in variables.items():
        template = template.replace('{' + key + '}', str(value or ''))
    return template


async def _notify(db, rule, incident, metric, name, details, now, *, recovery=False):
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    from app.services.regional_time import notification_branding
    if not recovery and not notifications_allowed(rule.schedule_start, rule.schedule_end,
                                                  rule.schedule_days, await get_configured_timezone(db)):
        return False
    if not recovery and rule.cooldown:
        last = (await db.execute(text('''SELECT max(triggered_at) FROM alerts
            WHERE rule_id=:rule AND id<>:id AND metadata->>'apm_event_key'=:key
              AND COALESCE(metadata->>'notified','true')='true' '''),
            {'rule': rule.id, 'id': incident.id, 'key': f'{metric}:{details["entity_id"]}'})).scalar()
        if last and (now-last).total_seconds() < rule.cooldown:
            return False
    at = now if recovery else incident.triggered_at
    brand = await notification_branding(db)
    duration = ap.duration_between(incident.triggered_at, now) if recovery else ''
    variables = {
        **ap.rule_phrasing(dict(rule._mapping), hostname=name, is_recovery=recovery, duration=duration),
        **phrasing(metric, name, details, recovery=recovery, duration=duration),
        'status_intro': 'The following alert has been resolved:' if recovery else 'An alert has been triggered:',
        'device_type': '', 'group': '', 'location': '', 'rtt': '', 'packet_loss': '',
    }
    variables.update(rule_name=rule.name, severity=rule.severity.upper(),
                     timestamp=brand.fmt_datetime(at, with_tz=True))
    templates = ap.effective_templates(dict(rule._mapping), 'device')
    prefix = 'recovery_' if recovery else ''
    context = {
        'subject': _render(templates[prefix + 'email_subject'], variables),
        'body': _render(templates[prefix + 'email_body'], variables),
        'message': _render(templates[prefix + 'sms_template'], variables),
        'hostname': name, 'ip_address': '', 'status': variables['status'],
        'severity': rule.severity, 'resolved': recovery, 'is_recovery': recovery,
        'details': [('Scenario' if metric == SYNTHETIC else 'SLO', name),
                    ('Failed step', details.get('failed_step') if not recovery else None),
                    ('Error', details.get('error') if not recovery else None),
                    ('Service', details.get('service')), ('Active for', variables['duration'] or None)],
        'triggered_at': at.isoformat(), 'first_seen': incident.triggered_at.isoformat(),
        'rule_id': str(rule.id), 'rule_name': rule.name, 'alert_id': str(incident.id),
        'delivery_event': 'recovery' if recovery else 'trigger',
        **alert_funnel.notification_fields(rule.severity,
            getattr(incident, 'priority', None) or getattr(rule, 'priority', None), trigger=not recovery),
    }
    await dispatch_to_channels(db, rule.notify_channels or [], context)
    return True


async def evaluate_event(db, metric, entity_id, name, breached: bool | None, details: dict) -> bool:
    """Called under the producer's cross-worker lock. Return central ownership.

    None means insufficient evidence: neither raise nor resolve. Recovery also
    closes old rule incidents after a rule is disabled, retargeted or deleted,
    but only current enabled matching rules can send notifications.
    """
    assert metric in EVENT_METRICS
    entity_id = str(entity_id)
    details = {**details, 'entity_id': entity_id}
    rules = (await db.execute(text('''SELECT * FROM alert_rules
        WHERE enabled = true AND metric = :metric AND
        (target IS NULL OR trim(target) = '' OR lower(target) = :target)'''),
        {'metric': metric, 'target': entity_id.lower()})).all()
    valid_rules = []
    for rule in rules:
        try:
            validate_definition(dict(rule._mapping))
            valid_rules.append(rule)
        except ValueError:
            logging.getLogger(__name__).warning('Unsupported APM event definition on rule %s', rule.id)
    rules = valid_rules
    rules_by_id = {str(rule.id): rule for rule in rules}
    if breached is None:
        return bool(rules)
    key = f'{metric}:{entity_id}'
    active = (await db.execute(text('''SELECT * FROM alerts
        WHERE status IN ('active','acknowledged') AND metadata->>'apm_event_key' = :key
        ORDER BY triggered_at'''), {'key': key})).all()
    now = datetime.now(timezone.utc)
    if not breached:
        for incident in active:
            notified = await ns.was_notified(db, incident.id)
            await db.execute(text("UPDATE alerts SET status='resolved', resolved_at=:now WHERE id=:id"),
                             {'id': incident.id, 'now': now})
            await db.commit()
            rule = rules_by_id.get(str(incident.rule_id))
            if rule and rule.recovery_alert and notified:
                await _notify(db, rule, incident, metric, name, details, now, recovery=True)
        await db.commit()
        return bool(rules)
    by_rule = {str(row.rule_id): row for row in active}
    for rule in rules:
        incident = by_rule.get(str(rule.id))
        message = phrasing(metric, name, details)['event_sentence']
        if incident is None:
            p_col, p_val, p_params = alert_funnel.priority_insert(
                await alert_funnel.priority_supported(db), getattr(rule, 'priority', None), rule.severity)
            incident = (await db.execute(text(f'''INSERT INTO alerts
                (rule_id,status,severity,message,triggered_at,metadata{p_col})
                VALUES (:rule,'active',:severity,:message,:now,CAST(:meta AS jsonb){p_val})
                RETURNING *'''), {
                **p_params, 'rule': rule.id, 'severity': rule.severity, 'message': message, 'now': now,
                'meta': json.dumps({**details, 'source': 'apm_rule_event', 'metric': metric,
                    'rule_id': str(rule.id), 'apm_event_key': key, 'entity_id': entity_id,
                    'entity_name': name, 'service': details.get('service') or name, 'notified': False}),
            })).first()
            await db.commit()
        else:
            await db.execute(text('''UPDATE alerts SET message=:message,
                metadata=metadata || CAST(:detail AS jsonb) WHERE id=:id'''),
                {'id': incident.id, 'message': message, 'detail': json.dumps(details)})
        if await ns.is_pending(db, incident.id) and await ns.claim_pending(db, incident.id):
            await db.commit()
            sent = await _notify(db, rule, incident, metric, name, details, now)
            await ns.stamp(db, incident.id, sent)
            await db.commit()
    await db.commit()
    return bool(rules)
