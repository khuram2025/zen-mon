"""Read-only NCCM setup checks. Saved prerequisites are not device certification."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
import time
from datetime import datetime, timezone

from app.core import crypto
from app.services.ncm_collection import KNOWN_HOSTS, PLATFORM_COMMANDS, STARTUP_COMMANDS


def support_details(vendor, platform, config_types=None):
    vendor = (vendor or '').strip().lower()
    types = config_types or ['running']
    if platform and platform != 'autodetect':
        available = ['running', 'startup'] if platform in STARTUP_COMMANDS else ['running']
        if platform not in PLATFORM_COMMANDS:
            return {'state': 'unsupported', 'message': 'No collector is implemented for the configured platform.', 'config_types': []}
        if any(t not in available for t in types):
            return {'state': 'unsupported', 'message': 'The selected collector cannot capture every required configuration type.', 'config_types': available}
        return {'state': 'implemented', 'message': 'Collector implemented; verify the exact OS, privileges and device contexts in the pilot.', 'config_types': available}
    if vendor.startswith('aruba') or vendor.startswith('f5'):
        return {'state': 'unsupported', 'message': 'No explicit NCCM collector is available for this vendor. Eligibility does not imply driver support.', 'config_types': []}
    return {'state': 'needs_detection', 'message': 'Select the exact platform or let the first capture detect an implemented collector. Vendor inventory alone does not certify support.', 'config_types': []}


def saved_trust(host, port=22, path=None):
    """Inspect saved keys only; never learn or approve a key over the network."""
    from paramiko import HostKeys
    path = Path(path or KNOWN_HOSTS)
    try:
        keys = HostKeys(str(path))
        name = str(host) if int(port or 22) == 22 else f'[{host}]:{int(port)}'
        if keys.lookup(name):
            return {'state': 'pass', 'message': 'A saved SSH host-key entry exists. The collector will verify the actual peer when connecting.'}
        return {'state': 'blocked', 'message': 'No saved SSH host key for this address and port. Ask an appliance administrator to verify the device fingerprint independently and register it.'}
    except FileNotFoundError:
        return {'state': 'blocked', 'message': 'The managed SSH trust file is missing. An appliance administrator must provision verified device keys.'}
    except Exception:
        # Key parser exceptions can contain file contents; never expose them.
        return {'state': 'blocked', 'message': 'The managed SSH trust file cannot be read or parsed. Ask an appliance administrator to repair its format and access.'}


@lru_cache(maxsize=2)
def _runtime_bucket(bucket):
    result = {}
    for key, unit in [('worker', 'zenplus-ncm-worker.service'), ('scheduler', 'zenplus-ncm-backup.timer'), ('delivery', 'zenplus-ncm-delivery.timer')]:
        try:
            proc = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=2)
            result[key] = 'active' if proc.returncode == 0 and proc.stdout.strip() == 'active' else 'inactive'
        except (OSError, subprocess.TimeoutExpired):
            result[key] = 'unknown'
    return result


def runtime_health():
    return {**_runtime_bucket(int(time.monotonic() // 30)), 'encryption': 'configured' if crypto.is_configured() else 'missing'}


def device_readiness(device, runtime, trust):
    support = support_details(device.get('vendor'), device.get('platform'), device.get('config_types'))
    checks = []

    def add(key, label, status, detail, required=True):
        checks.append({'id': key, 'label': label, 'status': status, 'detail': detail, 'required': required})

    add('eligibility', 'Backup scope', 'pass' if device['eligible'] else 'blocked', device['eligibility_reason'])
    add('driver', 'Collector capability', 'blocked' if support['state'] == 'unsupported' else 'pass' if support['state'] == 'implemented' else 'pending', support['message'])
    add('profile', 'Connection profile', 'pass' if device.get('profile_configured') else 'blocked',
        'A connection profile with a username and password is saved. Authentication is verified by a capture.' if device.get('profile_configured') else 'Create or select a connection profile with device SSH credentials, then save the device settings.')
    add('enabled', 'Collection enabled', 'pass' if device.get('ncm_enabled') else 'blocked',
        'Collection is enabled.' if device.get('ncm_enabled') else 'Save an enabled enrollment before starting the first capture.')
    add('trust', 'Saved SSH trust', trust['state'], trust['message'])
    add('encryption', 'Archive encryption', 'pass' if runtime['encryption'] == 'configured' else 'blocked',
        'Archive encryption is configured. Maintain a protected recovery copy of the key.' if runtime['encryption'] == 'configured' else 'An appliance administrator must configure a valid archive encryption key.')
    add('worker', 'Backup worker', 'pass' if runtime['worker'] == 'active' else 'blocked',
        'The backup worker is active.' if runtime['worker'] == 'active' else 'The backup worker is unavailable or its status could not be checked. Ask an appliance administrator to inspect it.')
    add('capture', 'Authentication, privileges and complete capture', 'pass' if device.get('fresh') else 'pending',
        'Fresh validated captures exist for all required configuration types. Restore testing remains separate.' if device.get('fresh') else 'Run the first backup after prerequisites pass. Only a completed validated capture establishes authentication, read privileges and artifact evidence.', False)
    scheduled = bool(device.get('schedule_enabled'))
    add('schedule', 'Recurring coverage', 'pass' if scheduled and runtime['scheduler'] == 'active' else 'pending',
        'A schedule is enabled and the scheduler timer is active.' if scheduled and runtime['scheduler'] == 'active' else 'Enable a schedule after the first validated capture and verify the scheduler is active.', False)
    add('notifications', 'Notification delivery worker', 'pass' if runtime['delivery'] == 'active' else 'pending',
        'The delivery timer is active; destination acceptance still needs verification.' if runtime['delivery'] == 'active' else 'The notification delivery timer is inactive or unavailable.', False)
    can_capture = not any(c['required'] and c['status'] == 'blocked' for c in checks)
    return {'device_id': device['device_id'], 'checks': checks, 'can_capture': can_capture,
            'support': support, 'capture_verified': bool(device.get('fresh')),
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'assessment': 'Saved settings and local prerequisites only; no device connection was attempted.'}
