"""Read-only NCCM setup checks. Saved prerequisites are not device certification."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.core import crypto
from app.services.ncm_collection import KNOWN_HOSTS, DRIVERS, STARTUP_COMMANDS, UNSUPPORTED


def support_details(vendor, platform, config_types=None):
    vendor = (vendor or '').strip().lower()
    types = config_types or ['running']
    if platform and platform != 'autodetect':
        if platform not in DRIVERS:
            return {'state': 'unsupported', 'message': 'No collector is implemented for the configured platform.', 'config_types': []}
        available = ['running', 'startup'] if platform in STARTUP_COMMANDS else ['running']
        if any(t not in available for t in types):
            return {'state': 'unsupported', 'message': 'The selected collector cannot capture every required configuration type.', 'config_types': available}
        return {'state': 'implemented', 'message': 'Collector implemented; verify the exact OS, privileges and device contexts in the pilot.', 'config_types': available}
    for name, reason in UNSUPPORTED.items():
        if vendor.startswith(name.lower()):
            return {'state': 'unsupported', 'message': f'No {name} collector is available: {reason}', 'config_types': []}
    if vendor.startswith('aruba'):
        return {'state': 'needs_detection', 'message': 'Select the exact Aruba driver (AOS-CX, ArubaOS-Switch or Mobility Controller); auto-detection cannot always tell them apart.', 'config_types': []}
    return {'state': 'needs_detection', 'message': 'Select the exact platform or let the first capture detect an implemented collector. Vendor inventory alone does not certify support.', 'config_types': []}


def saved_trust(host, port=22, path=None):
    """Inspect the appliance trust file only; never learn or approve a key over the network."""
    from paramiko import HostKeys
    path = Path(path or KNOWN_HOSTS)
    try:
        keys = HostKeys(str(path))
        name = str(host) if int(port or 22) == 22 else f'[{host}]:{int(port)}'
        if keys.lookup(name):
            return {'state': 'pass', 'source': 'appliance_file', 'message': 'A saved SSH host-key entry exists in the appliance trust file. The collector will verify the actual peer when connecting.'}
        return {'state': 'blocked', 'source': None, 'missing': True, 'message': 'No saved SSH host key for this address and port. Read the fingerprint from the device console, then register it under SSH trust (or ask an appliance administrator).'}
    except FileNotFoundError:
        return {'state': 'blocked', 'source': None, 'missing': True, 'message': 'No SSH trust is registered for this device. Read the fingerprint from the device console, then register it under SSH trust.'}
    except Exception:
        # Key parser exceptions can contain file contents; never expose them.
        return {'state': 'blocked', 'source': None, 'message': 'The appliance SSH trust file cannot be read or parsed. Register the device key under SSH trust or ask an administrator to repair the file.'}


async def managed_keys(db, host, port):
    rows = (await db.execute(text('''SELECT id,key_type,key_base64,fingerprint_sha256,registered_by_name,created_at,verification_method
        FROM ncm_host_keys WHERE host=:host AND port=:port AND retired_at IS NULL'''),
        {'host': str(host), 'port': int(port or 22)})).fetchall()
    return rows


async def trust_state(db, host, port, *, auto_trust=False):
    """Managed (database) trust first, then the appliance trust file."""
    if host:
        rows = await managed_keys(db, host, port)
        accepted = [r for r in rows if auto_trust or r.verification_method != 'first_use']
        if accepted:
            r = accepted[0]
            if all(k.verification_method == 'first_use' for k in accepted):
                return {'state': 'pass', 'source': 'first_use', 'fingerprint': r.fingerprint_sha256,
                        'message': f'Automatically learned—not console verified ({r.fingerprint_sha256}). Future connections must match the saved key; changed keys are blocked.'}
            return {'state': 'pass', 'source': 'managed', 'fingerprint': r.fingerprint_sha256,
                    'message': f'Managed SSH trust registered by {r.registered_by_name} ({r.fingerprint_sha256}). The collector verifies the actual peer when connecting.'}
    import asyncio
    saved = await asyncio.to_thread(saved_trust, host, port)
    if saved['state'] == 'pass' or not saved.get('missing') or not host:
        return saved
    if rows:
        return {'state': 'blocked', 'source': 'first_use',
                'message': 'This key was automatically learned. Strict verification requires independent fingerprint registration under SSH trust.'}
    history = (await db.execute(text('SELECT 1 FROM ncm_host_keys WHERE host=:h AND port=:p LIMIT 1'),
                               {'h': str(host), 'p': int(port or 22)})).first()
    if history:
        return {'state': 'blocked', 'source': None,
                'message': 'SSH trust was retired for this address and port. Register a verified key under SSH trust; automatic relearning is blocked.'}
    if auto_trust:
        return {'state': 'pending', 'source': 'first_use',
                'message': 'Automatically trust new devices is enabled. The first backup will save the presented key without independent verification. Changed keys will be blocked.'}
    return saved


@lru_cache(maxsize=2)
def _runtime_bucket(bucket):
    result = {}
    for key, unit in [('worker', 'zenplus-ncm-worker.service'), ('scheduler', 'zenplus-ncm-backup.timer'), ('delivery', 'zenplus-ncm-delivery.timer')]:
        try:
            proc = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=2)
            state = proc.stdout.strip()
            result[key] = 'active' if state == 'active' else 'inactive' if state in ('inactive', 'failed', 'activating', 'deactivating') else 'unknown'
        except (OSError, subprocess.TimeoutExpired):
            result[key] = 'unknown'
    try:
        proc = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '-p', 'NTP', '-p', 'Timezone'],
                              capture_output=True, text=True, timeout=2)
        values = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
        synced = values.get('NTPSynchronized')
        result['time_sync'] = 'synchronized' if synced == 'yes' else 'unsynchronized' if synced == 'no' else 'unknown'
        result['ntp_service'] = 'active' if values.get('NTP') == 'yes' else 'inactive' if values.get('NTP') == 'no' else 'unknown'
        result['appliance_timezone'] = values.get('Timezone') or None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        result.update(time_sync='unknown', ntp_service='unknown', appliance_timezone=None)
    return result


def runtime_health():
    return {**_runtime_bucket(int(time.monotonic() // 30)), 'encryption': 'configured' if crypto.is_configured() else 'missing',
            'server_time': datetime.now(timezone.utc).isoformat()}


def device_readiness(device, runtime, trust):
    support = support_details(device.get('vendor'), device.get('platform'), device.get('config_types'))
    checks = []

    def add(key, label, status, detail, required=True, action=None):
        checks.append({'id': key, 'label': label, 'status': status, 'detail': detail, 'required': required, 'action': action})

    # Eligibility decides COVERAGE (the protection denominator), never whether a
    # deliberate backup may run: an operator can protect a device outside the
    # automatic scope; it just does not count until included.
    add('eligibility', 'Coverage scope', 'pass' if device['eligible'] else 'info',
        device['eligibility_reason'] + ('' if device['eligible'] else ' Backups can still run; they are not counted in coverage.'),
        False, None if device['eligible'] else 'scope')
    add('driver', 'Collector capability', 'blocked' if support['state'] == 'unsupported' else 'pass' if support['state'] == 'implemented' else 'pending', support['message'],
        action='settings' if support['state'] != 'implemented' else None)
    add('profile', 'Connection profile', 'pass' if device.get('profile_configured') else 'blocked',
        'A connection profile with a username and a password or SSH key is saved. Authentication is verified by a capture.' if device.get('profile_configured') else 'Create or select a connection profile with device SSH credentials, then save the device settings.',
        action=None if device.get('profile_configured') else 'profile')
    add('enabled', 'Collection enabled', 'pass' if device.get('ncm_enabled') else 'blocked',
        'Collection is enabled.' if device.get('ncm_enabled') else 'Save an enabled enrollment before starting the first capture.',
        action=None if device.get('ncm_enabled') else 'settings')
    add('trust', 'SSH host trust', trust['state'], trust['message'], action=None if trust['state'] == 'pass' else 'trust')
    add('encryption', 'Archive encryption', 'pass' if runtime['encryption'] == 'configured' else 'blocked',
        'Archive encryption is configured. Maintain a protected recovery copy of the key.' if runtime['encryption'] == 'configured' else 'An appliance administrator must configure a valid archive encryption key.')
    worker = runtime.get('worker')
    add('worker', 'Backup worker', 'pass' if worker == 'active' else 'blocked' if worker == 'inactive' else 'pending',
        'The backup worker is active.' if worker == 'active' else 'The backup worker service is not running. Ask an appliance administrator to start zenplus-ncm-worker.' if worker == 'inactive' else 'The backup worker status could not be checked; queued jobs run once a worker is available.')
    add('capture', 'Authentication, privileges and complete capture', 'pass' if device.get('fresh') else 'pending',
        'Fresh validated captures exist for all required configuration types. Restore testing remains separate.' if device.get('fresh') else 'Run the first backup after prerequisites pass. Only a completed validated capture establishes authentication, read privileges and artifact evidence.', False)
    scheduled = bool(device.get('schedule_enabled'))
    add('schedule', 'Recurring coverage', 'pass' if scheduled and runtime['scheduler'] == 'active' else 'pending',
        'A schedule is enabled and the scheduler timer is active.' if scheduled and runtime['scheduler'] == 'active' else 'Enable a schedule after the first validated capture and verify the scheduler is active.', False,
        None if scheduled else 'settings')
    add('notifications', 'Notification delivery worker', 'pass' if runtime['delivery'] == 'active' else 'pending',
        'The delivery timer is active; destination acceptance still needs verification.' if runtime['delivery'] == 'active' else 'The notification delivery timer is inactive or unavailable.', False)
    if runtime.get('time_sync') == 'unsynchronized':
        add('time', 'Appliance clock', 'pending', 'The appliance clock is not NTP-synchronized. Schedules, freshness and job ages depend on it; ask an administrator to repair time synchronization.', False)
    blockers = [c for c in checks if c['required'] and c['status'] == 'blocked']
    return {'device_id': device['device_id'], 'checks': checks, 'can_capture': not blockers,
            'blockers': [{'id': c['id'], 'label': c['label'], 'detail': c['detail']} for c in blockers],
            'support': support, 'capture_verified': bool(device.get('fresh')),
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'assessment': 'Saved settings and local prerequisites only; no device connection was attempted.'}
