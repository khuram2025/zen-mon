"""Secrets for already authorized, assigned service checks; never log their values."""
import re
from app.core.crypto import decrypt


def supports_service_auth(version):
    match = re.fullmatch(r'(?:sensor-)?v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})(?:[-+].*)?', version or '')
    return bool(match and tuple(map(int, match.groups())) >= (1, 23, 5))


def service_auth_config(row):
    result = {
        'credential_id': str(row['credential_id']) if row.get('credential_id') else None,
        'credential_auth_type': row.get('credential_auth_type') or '',
        'credential_username': row.get('credential_username') or '',
        'credential_secret': '', 'credential_error': '',
        'workflow_operator': row.get('workflow_operator') or 'all',
        'workflow_steps': row.get('workflow_steps') or [],
    }
    if row.get('credential_id'):
        if not row.get('credential_auth_type'):
            result['credential_error'] = 'Service credential is unavailable'
        else:
            try:
                result['credential_secret'] = decrypt(row.get('secret_cipher')) or ''
            except Exception:
                result['credential_error'] = 'Service credential could not be decrypted'
    return result
