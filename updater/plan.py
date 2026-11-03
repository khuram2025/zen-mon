"""Read-only offer previews and bounded, explicitly selected installation paths."""
from __future__ import annotations

import hashlib
import json
import re
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from .version_policy import validate_transition, version_key

MAX_STEPS = 32
MANUAL_STATUS = Path('/opt/zenplus/updater/logs/manual-install.json')


def write_manual_status(state, target, message):
    """Small public result record; no enrollment credentials or package URLs."""
    MANUAL_STATUS.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.manual-install-', dir=MANUAL_STATUS.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'state': state, 'target_version': target, 'message': message,
                       'updated_at': datetime.now(timezone.utc).isoformat()}, stream)
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, MANUAL_STATUS)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def release_identity(release):
    identity = {k: release.get(k) for k in ('id', 'version', 'min_version', 'package_sha256')}
    identity['min_version'] = identity['min_version'] or None
    if not isinstance(identity['id'], str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', identity['id']):
        raise ValueError('Update server returned an invalid release identity')
    if not isinstance(identity['package_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', identity['package_sha256']):
        raise ValueError('Update server returned an invalid package checksum')
    version_key(identity['version'])
    return identity


def get_plan(cfg, *, current=None, client_factory=None):
    """GET offers at successive virtual versions; never check in or report progress.

    The catalog's GET check changes only its request-local appliance object.
    Every bridge retains its existing rollout and previous-failure restrictions.
    """
    from .agent import _api_client
    from .inventory import get_arch, get_current_version
    current = current or get_current_version()
    version_key(current)
    cursor = current
    steps = []
    with (client_factory or _api_client)(cfg) as client:
        for _ in range(MAX_STEPS + 1):
            response = client.get('/api/v1/updates/check', params={'current_version': cursor, 'arch': get_arch()})
            response.raise_for_status()
            data = response.json()
            if data.get('available') is False and data.get('release') is None:
                break
            release = data.get('release')
            if data.get('available') is not True or not isinstance(release, dict):
                raise ValueError('Update server returned an invalid availability response')
            if len(steps) >= MAX_STEPS:
                raise ValueError('Upgrade path exceeds the supported number of steps')
            validate_transition(release, cursor)
            steps.append({**release_identity(release),
                          'changelog': str(release.get('changelog') or '')[:12000],
                          'severity': str(release.get('severity') or 'normal')[:30]})
            cursor = release['version']
    identities = [release_identity(step) for step in steps]
    digest = hashlib.sha256(json.dumps({'from_version': current, 'steps': identities}, sort_keys=True).encode()).hexdigest()
    return {'from_version': current, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'plan_id': digest, 'steps': steps}


def selected_path(plan, target):
    for index, step in enumerate(plan['steps']):
        if step['version'] == target:
            return [release_identity(s) for s in plan['steps'][:index + 1]]
    raise ValueError('Selected version is no longer on the eligible upgrade path; check again')


def validate_remaining(steps, current):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError('Invalid approved upgrade path')
    for step in steps:
        release_identity(step)
        validate_transition(step, current)
        current = step['version']
    return steps


def automatic_install_allowed(config, now=None):
    """Scheduled runs check only unless enabled and inside the local-time window."""
    if not config.auto_update:
        return False
    start, end = config.maintenance_window_start, config.maintenance_window_end
    if not start and not end:
        return True
    if not all(re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value or '') for value in (start, end)):
        return False
    clock = (now or datetime.now().astimezone()).strftime('%H:%M')
    if start == end:
        return True
    return start <= clock < end if start < end else clock >= start or clock < end
