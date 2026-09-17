"""NCM delivery acknowledges provider acceptance, never just an attempt.

At-least-once: a process crash after acceptance and before commit can duplicate
delivery. event_id is the receiver's stable deduplication key.
"""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from sqlalchemy import text
from app.core.crypto import decrypt_config
from app.services.notification_transport import NotificationHTTPClient

SUPPORTED_CHANNELS = ('email', 'webhook', 'slack', 'teams', 'discord', 'pagerduty')


def smtp_send(config, recipients, payload):
    targets = [x.strip() for x in recipients.split(',') if x.strip()]
    if not config.get('host') or not config.get('from_email') or not targets:
        raise ValueError('Incomplete SMTP configuration')
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = config['from_email'], ', '.join(targets), payload['subject']
    msg['Message-ID'] = f"<ncm-{payload['event_id']}@zenplus.local>"
    msg.set_content(payload['body'])
    context = ssl.create_default_context()
    implicit = config.get('encryption') == 'ssl'
    cls = smtplib.SMTP_SSL if implicit else smtplib.SMTP
    kwargs = {'context': context} if implicit else {}
    with cls(config['host'], int(config.get('port') or (465 if implicit else 587)), timeout=15, **kwargs) as client:
        if not implicit:
            client.starttls(context=context)
        if config.get('username'):
            client.login(config['username'], config.get('password', ''))
        if client.send_message(msg):
            raise ValueError('One or more SMTP recipients rejected')


async def send(db, channel_ids, payload):
    accepted = 0
    for channel_id in channel_ids:
        ch = (await db.execute(text('SELECT type,config,gateway_id,enabled FROM notification_channels WHERE id=:id'),
                               {'id': channel_id})).first()
        if not ch or not ch.enabled or ch.type not in SUPPORTED_CHANNELS:
            raise ValueError('Unavailable notification channel')
        cfg = decrypt_config(ch.config)
        if ch.type == 'email':
            from app.services.host_alert_service import _gateway_config
            gw = await _gateway_config(db, ch, 'smtp')
            if not gw:
                raise ValueError('Unavailable SMTP gateway')
            await asyncio.to_thread(smtp_send, gw, cfg.get('recipients', ''), payload)
        else:
            url = cfg.get('url') or cfg.get('webhook_url')
            body = payload
            headers = {'X-ZenPlus-Event-ID': payload['event_id']}
            if ch.type == 'webhook':
                headers.update(cfg.get('headers') or {})
                if cfg.get('auth_bearer'):
                    headers['Authorization'] = 'Bearer ' + cfg['auth_bearer']
            elif ch.type == 'slack':
                body = {'text': payload['subject'] + '\n' + payload['body']}
            elif ch.type == 'teams':
                body = {'text': payload['subject'] + '\n' + payload['body']}
            elif ch.type == 'discord':
                body = {'content': (payload['subject'] + '\n' + payload['body'])[:2000]}
            elif ch.type == 'pagerduty':
                key = cfg.get('routing_key') or cfg.get('integration_key')
                if not key:
                    raise ValueError('Missing routing key')
                url = 'https://events.pagerduty.com/v2/enqueue'
                body = {'routing_key': key, 'event_action': 'trigger', 'dedup_key': payload['event_id'],
                        'payload': {'summary': payload['subject'], 'source': payload['hostname'], 'severity': 'warning'}}
            if not url:
                raise ValueError('Missing delivery URL')
            async with NotificationHTTPClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(url, json=body, headers=headers)
                response.raise_for_status()
                if ch.type == 'slack' and response.text.strip() != 'ok':
                    raise ValueError('Provider rejected event')
                if ch.type == 'pagerduty' and response.json().get('status') != 'success':
                    raise ValueError('Provider rejected event')
        accepted += 1
    return accepted
