"""Event-triggered configuration backup.

Configuration-change evidence arrives from device syslog (network_events),
SNMP traps (evaluate-trap hook) or an authorised API caller (e.g. a change
management system). Events are debounced per device: a backup is queued
after a quiet period, or after a bounded delay during a long burst, and the
per-device job constraint serialises captures. The captured version is later
linked to the events that triggered it.

Attribution is recorded ONLY when the device message explicitly names the
user; nothing is inferred from a configuration difference.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

QUIET_PERIOD = timedelta(minutes=2)
MAX_DELAY = timedelta(minutes=15)
SYSLOG_LOOKBACK = timedelta(minutes=15)

# (vendor label, message pattern, user pattern)
SYSLOG_PATTERNS = [
    ('Cisco/Arista', re.compile(r'%(?:SYS-5-CONFIG_I|SYS-5-CONFIG_E|VSHD-5-VSHD_SYSLOG_CONFIG_I|CONFIG_SESSION_COMMIT_SUCCESS)\b'),
     re.compile(r'\bby\s+([\w.@\\-]{1,64})')),
    ('Cisco ASA', re.compile(r'%ASA-\d-11100[58]\b|%ASA-\d-111010\b'), re.compile(r"User '([^']{1,64})'")),
    ('Junos', re.compile(r'\bUI_COMMIT(?:_COMPLETED)?\b'), re.compile(r"User '([^']{1,64})'")),
    ('FortiOS', re.compile(r'logid="?01000445(?:44|46|47)"?'), re.compile(r'\buser="([^"]{1,64})"')),
    ('Comware/VRP', re.compile(r'CFGMAN_CFGCHANGED|SHELL_CMD_CONFIG|CFM_TRANSTOCONFIG|CONFIG_CHANGE\b'), re.compile(r'\b[Uu]ser(?:\s*name)?[=:\s]+"?([\w.@\\-]{1,64})')),
    ('PAN-OS', re.compile(r'\bCONFIG\b.*\bcommit\b|[Cc]ommit job \d+ (?:succeeded|completed)'), re.compile(r'\badmin(?:istrator)?[=:\s]+"?([\w.@\\-]{1,64})')),
    ('Generic', re.compile(r'(?i)\bconfiguration\s+(?:was\s+)?(?:changed|modified|committed)\b'), re.compile(r'(?i)\bby\s+(?:user\s+)?"?([\w.@\\-]{1,64})')),
]

# Traps that announce a configuration change. Exact OID or dotted prefix.
CHANGE_TRAP_OIDS = {
    '1.3.6.1.4.1.9.9.43.2.0.1': 'CISCO-CONFIG-MAN-MIB ciscoConfigManEvent',
    '1.3.6.1.4.1.9.9.43.2.0.2': 'CISCO-CONFIG-MAN-MIB ccmCLIRunningConfigChanged',
    '1.3.6.1.4.1.2636.4.5.0.1': 'JUNIPER-CFGMGMT-MIB jnxCmCfgChange',
    '1.3.6.1.4.1.2011.6.10.2.1': 'HUAWEI-CONFIG-MAN-MIB hwCfgManEventlog',
    '1.3.6.1.4.1.25506.2.4.2.1': 'HH3C-CONFIG-MAN-MIB hh3cCfgManEventlog',
}


def classify_syslog(message):
    """Return (summary, reported_user) when the message announces a
    configuration change, else None."""
    for vendor, pattern, user_pattern in SYSLOG_PATTERNS:
        if pattern.search(message or ''):
            user = user_pattern.search(message)
            return f'{vendor} configuration-change message', (user.group(1) if user else None)
    return None


def change_trap(oid):
    oid = (oid or '').strip().lstrip('.')
    for known, label in CHANGE_TRAP_OIDS.items():
        if oid == known or oid.startswith(known + '.'):
            return label
    return None


async def scan_syslog(db, now=None):
    """Turn recent syslog messages from enrolled devices into change events."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(text('''SELECT e.id,e.device_id,e.received_at,e.message FROM network_events e
        JOIN device_ncm n ON n.device_id=e.device_id
        WHERE e.received_at>:since AND n.enabled AND n.event_backup
          AND NOT EXISTS(SELECT 1 FROM ncm_change_events c WHERE c.source='syslog' AND c.source_ref=e.id::text)
        ORDER BY e.received_at LIMIT 5000'''), {'since': now - SYSLOG_LOOKBACK})).fetchall()
    found = 0
    for r in rows:
        hit = classify_syslog(r.message)
        if not hit:
            continue
        summary, user = hit
        await db.execute(text('''INSERT INTO ncm_change_events(device_id,source,source_ref,received_at,summary,reported_user)
            VALUES(:d,'syslog',:ref,:at,:s,:u) ON CONFLICT(source,source_ref) DO NOTHING'''),
            {'d': r.device_id, 'ref': str(r.id), 'at': r.received_at, 's': summary, 'u': user})
        found += 1
    return found


async def record_trap(db, device_id, trap_oid, event_id=None, received_at=None):
    label = change_trap(trap_oid)
    if not label or not device_id:
        return False
    enrolled = (await db.execute(text('SELECT 1 FROM device_ncm WHERE device_id=:d AND enabled AND event_backup'), {'d': device_id})).first()
    if not enrolled:
        return False
    await db.execute(text('''INSERT INTO ncm_change_events(device_id,source,source_ref,received_at,summary)
        VALUES(:d,'trap',:ref,:at,:s) ON CONFLICT(source,source_ref) DO NOTHING'''),
        {'d': device_id, 'ref': str(event_id or f'{trap_oid}:{datetime.now(timezone.utc).timestamp()}'),
         'at': received_at or datetime.now(timezone.utc), 's': f'{label} trap'})
    return True


async def process_pending(db, now=None):
    """Queue a debounced backup for each device with pending change events."""
    from app.services.ncm_jobs import enqueue
    from app.services.ncm_readiness import runtime_health
    from app.api.v1.ncm import _device_summary, _readiness
    import asyncio
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(text('''SELECT device_id,min(received_at) AS first_at,max(received_at) AS last_at,count(*) AS n
        FROM ncm_change_events WHERE processed_at IS NULL GROUP BY device_id'''))).fetchall()
    queued = skipped = 0
    runtime = None
    for r in rows:
        if not (r.last_at <= now - QUIET_PERIOD or r.first_at <= now - MAX_DELAY):
            continue  # still inside the burst
        active = (await db.execute(text("SELECT id,status FROM ncm_jobs WHERE device_id=:d AND status IN ('queued','running')"),
                                   {'d': r.device_id})).first()
        if active and active.status == 'running':
            continue  # the running capture may predate the change; wait for it to finish
        runtime = runtime or await asyncio.to_thread(runtime_health)
        device = await _device_summary(db, r.device_id, None)
        readiness = await _readiness(db, device, runtime) if device else None
        if not readiness or not readiness['can_capture']:
            await db.execute(text('''UPDATE ncm_change_events SET processed_at=now() WHERE device_id=:d AND processed_at IS NULL'''),
                             {'d': r.device_id})
            skipped += 1
            continue
        result = await enqueue(db, r.device_id, trigger='event')
        await db.execute(text('''UPDATE ncm_change_events SET processed_at=now(),job_id=:job
            WHERE device_id=:d AND processed_at IS NULL'''), {'d': r.device_id, 'job': result['job_id']})
        queued += int(result['queued'])
    await db.commit()
    return {'devices': len(rows), 'queued': queued, 'skipped_blocked': skipped}
