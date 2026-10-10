"""One verified HTTP request path for gateway tests and SMS delivery."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, quote_plus

from app.services.alert_delivery_log import DeliveryRejected
from app.services.notification_transport import NotificationHTTPClient


def fill_template(template: str, values: dict, *, is_get: bool,
                  content_type: str = '') -> str:
    media_type = content_type.split(';', 1)[0].strip().lower()
    def replace(match):
        key = match.group(1)
        if key not in values:
            return match.group(0)
        rendered = str(values[key])
        if is_get:
            # Keep existing multi-recipient GET conventions compatible.
            rendered = quote(rendered, safe=',+') if key == 'recipients' else quote(rendered, safe='')
        elif media_type == 'application/x-www-form-urlencoded':
            rendered = quote_plus(rendered, safe='')
        elif media_type == 'application/json':
            # JSON templates place substitutions inside quoted strings.
            rendered = json.dumps(rendered, ensure_ascii=False)[1:-1]
        return rendered
    return re.sub(r'\{([a-z_]+)\}', replace, template)


def ensure_sms_accepted(response) -> None:
    status = response.status_code
    if not 200 <= status < 300:
        raise DeliveryRejected(f'SMS provider rejected request (HTTP {status})')
    try:
        body = response.json()
    except ValueError:
        return  # A custom HTTP provider may return plain text or no body.
    if isinstance(body, dict):
        # Some SMS APIs report rejection in a 200 response. Do not echo a
        # provider body: it may contain recipient numbers or credentials.
        for key in ('success', 'Success'):
            if key in body and str(body[key]).lower() in ('false', '0'):
                raise DeliveryRejected('SMS provider did not accept the message')


async def send_sms_request(config: dict, values: dict, *, client_factory=None):
    if config.get('provider', 'custom_http') != 'custom_http':
        raise ValueError('This SMS provider does not support sending yet')
    if not config.get('api_url'):
        raise ValueError('SMS gateway API URL is not configured')
    if not str(values.get('recipients') or '').strip():
        raise ValueError('A recipient phone number is required')
    method = str(config.get('http_method') or 'GET').upper()
    if method not in ('GET', 'POST'):
        raise ValueError('SMS gateway method must be GET or POST')
    headers = dict(config.get('custom_headers') or {})
    content_type = str(config.get('content_type') or '')
    if not content_type:
        content_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '')
    elif method == 'POST':
        headers = {k: v for k, v in headers.items() if k.lower() != 'content-type'}
        headers['Content-Type'] = content_type
    media_type = content_type.split(';', 1)[0].strip().lower()
    template = fill_template(config.get('request_template') or '', values,
                             is_get=method == 'GET', content_type=content_type)
    payload = None
    if method == 'POST' and media_type == 'application/json':
        try:
            payload = json.loads(template)
        except ValueError:
            raise ValueError('SMS gateway JSON request template is invalid') from None
    auth = None
    if config.get('auth_type') == 'basic':
        auth = (config.get('auth_username') or '', config.get('auth_password') or '')
    elif config.get('auth_type') == 'bearer' and config.get('auth_token_value'):
        headers = {k: v for k, v in headers.items() if k.lower() != 'authorization'}
        headers['Authorization'] = 'Bearer ' + config['auth_token_value']
    factory = client_factory or NotificationHTTPClient
    async with factory(timeout=15.0, verify=True) as client:
        if method == 'POST':
            if media_type == 'application/json':
                response = await client.post(config['api_url'], json=payload, headers=headers, auth=auth)
            else:
                response = await client.post(config['api_url'], content=template, headers=headers, auth=auth)
        else:
            url = config['api_url']
            if template:
                url += ('&' if '?' in url else '?') + template
            response = await client.get(url, headers=headers, auth=auth)
    ensure_sms_accepted(response)
    return response
