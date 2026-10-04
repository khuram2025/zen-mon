"""Network-configuration compliance: policy packs evaluated against captures.

Rules are regular expressions applied to the latest VALIDATED running
configuration of each enrolled device. Check types:

* must_match / must_not_match       — anywhere in the configuration
* section_must_match                — every section whose header matches
                                      `section_pattern` must contain `pattern`
* section_must_not_match            — no matching section may contain it
* section_any_must_match            — at least one matching section contains it

A section is a header line plus the following lines indented deeper than it
(Cisco/Arista/Aruba blocks, FortiOS `config`/`edit` blocks). Evidence is
redacted line-by-line before it is stored, so results never carry secrets.

Outcomes: pass, fail, unknown (no validated configuration / platform not
resolved) and excepted (an active, owned, expiring exception). Evaluation
never contacts devices.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from sqlalchemy import text

from app.services.ncm_content import read_content, redact_config, safe_pattern

SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
CHECK_TYPES = ('must_match', 'must_not_match', 'section_must_match', 'section_must_not_match', 'section_any_must_match')

_CISCO = ['cisco_ios', 'cisco_xe']


def _r(key, title, severity, check, pattern, remediation, section=None, description=None):
    return dict(rule_key=key, title=title, severity=severity, check_type=check, pattern=pattern,
                section_pattern=section, remediation=remediation, description=description)


# Starter packs: widely accepted hardening checks. They are deliberately
# conservative and are a starting point to review, not a certification.
BUILTIN_POLICIES = [
    dict(key='cisco-ios-hardening', version=1, platforms=_CISCO, name='Cisco IOS / IOS-XE hardening',
         description='Management-plane hardening checks for Cisco IOS and IOS-XE (starter pack; review before relying on it).',
         rules=[
             _r('ssh-v2', 'SSH version 2 only', 'medium', 'must_match', r'^ip ssh version 2\b', 'Configure "ip ssh version 2".'),
             _r('vty-no-telnet', 'VTY lines do not accept Telnet', 'high', 'section_must_not_match', r'^\s+transport input\b.*\b(?:telnet|all)\b',
                'Set "transport input ssh" on every "line vty" block.', section=r'^line vty\b'),
             _r('vty-acl', 'VTY lines restrict source addresses', 'medium', 'section_must_match', r'^\s+access-class\s+\S+\s+in\b',
                'Apply an inbound "access-class" to every "line vty" block.', section=r'^line vty\b'),
             _r('password-encryption', 'Service password-encryption enabled', 'medium', 'must_match', r'^service password-encryption\b',
                'Configure "service password-encryption".'),
             _r('no-enable-password', 'No plain "enable password"', 'high', 'must_not_match', r'^enable password\b',
                'Replace "enable password" with "enable secret" (type 8/9).'),
             _r('aaa-new-model', 'AAA enabled', 'medium', 'must_match', r'^aaa new-model\b', 'Configure "aaa new-model" with central authentication.'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^logging (?:host\s+)?\d{1,3}(?:\.\d{1,3}){3}\b|^logging host\s',
                'Configure "logging host <collector>".'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^ntp server\s', 'Configure at least one "ntp server".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match', r'^snmp-server community (?:public|private)\b',
                'Remove "public"/"private" communities; prefer SNMPv3.'),
             _r('http-server', 'Plain HTTP management disabled', 'low', 'must_not_match', r'^ip http server\b', 'Configure "no ip http server".'),
             _r('login-banner', 'Login banner present', 'low', 'must_match', r'^banner (?:login|motd)\b', 'Configure a legal login banner.'),
         ]),
    dict(key='cisco-nxos-hardening', version=1, platforms=['cisco_nxos'], name='Cisco NX-OS hardening',
         description='Management-plane hardening checks for Cisco NX-OS (starter pack).',
         rules=[
             _r('no-telnet', 'Telnet feature disabled', 'high', 'must_not_match', r'^feature telnet\b', 'Configure "no feature telnet".'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^ntp server\s', 'Configure at least one "ntp server".'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^logging server\s', 'Configure "logging server <collector>".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match', r'^snmp-server community (?:public|private)\b',
                'Remove default communities; prefer SNMPv3 users.'),
         ]),
    dict(key='cisco-asa-hardening', version=1, platforms=['cisco_asa'], name='Cisco ASA hardening',
         description='Management-plane hardening checks for Cisco ASA (starter pack).',
         rules=[
             _r('no-telnet', 'Telnet management disabled', 'high', 'must_not_match', r'^telnet \d', 'Remove "telnet" management entries; use SSH.'),
             _r('ssh-v2', 'SSH version 2 only', 'medium', 'must_match', r'^ssh version 2\b', 'Configure "ssh version 2".'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^logging host\s', 'Configure "logging host <if> <collector>".'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^ntp server\s', 'Configure at least one "ntp server".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match', r'^snmp-server (?:host .* )?community (?:public|private)\b',
                'Remove default communities; prefer SNMPv3.'),
         ]),
    dict(key='arista-eos-hardening', version=1, platforms=['arista_eos'], name='Arista EOS hardening',
         description='Management-plane hardening checks for Arista EOS (starter pack).',
         rules=[
             _r('no-telnet', 'Telnet management disabled', 'high', 'section_must_not_match', r'^\s+no shutdown\b',
                'Shut down "management telnet".', section=r'^management telnet\b'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^ntp server\s', 'Configure at least one "ntp server".'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^logging host\s', 'Configure "logging host <collector>".'),
             _r('aaa', 'Central AAA login configured', 'medium', 'must_match', r'^aaa authentication login\b', 'Configure "aaa authentication login".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match', r'^snmp-server community (?:public|private)\b',
                'Remove default communities; prefer SNMPv3.'),
         ]),
    dict(key='fortios-hardening', version=1, platforms=['fortinet'], name='Fortinet FortiOS hardening',
         description='Administrative-access hardening checks for FortiGate (starter pack).',
         rules=[
             _r('no-cleartext-admin', 'No Telnet/HTTP administrative access on interfaces', 'high', 'must_not_match',
                r'^\s*set allowaccess\b.*\b(?:telnet|http)\b', 'Remove "telnet" and "http" from interface "allowaccess".'),
             _r('ntp-sync', 'NTP synchronization enabled', 'medium', 'section_must_match', r'^\s*set ntpsync enable\b',
                'Enable "set ntpsync enable" under "config system ntp".', section=r'^config system ntp\b'),
             _r('remote-logging', 'Remote logging enabled', 'medium', 'section_any_must_match', r'^\s*set status enable\b',
                'Enable a syslog or FortiAnalyzer log destination.', section=r'^config log (?:syslogd|fortianalyzer)\d? setting\b'),
             _r('strong-crypto', 'Strong cryptography enforced', 'medium', 'must_match', r'^\s*set strong-crypto enable\b',
                'Set "strong-crypto enable" under "config system global".'),
             _r('admin-timeout', 'Admin idle timeout 15 minutes or less', 'low', 'must_match', r'^\s*set admintimeout (?:[1-9]|1[0-5])\s*$',
                'Set "admintimeout" to 15 or less under "config system global".'),
             _r('pre-login-banner', 'Pre-login banner enabled', 'low', 'must_match', r'^\s*set pre-login-banner enable\b',
                'Set "pre-login-banner enable" under "config system global".'),
         ]),
    dict(key='panos-hardening', version=1, platforms=['paloalto_panos'], name='Palo Alto PAN-OS hardening',
         description='Management-plane hardening checks for PAN-OS running configuration (starter pack).',
         rules=[
             _r('no-telnet', 'Telnet management disabled', 'high', 'must_match', r'disable-telnet\s+yes', 'Disable Telnet on the management interface.'),
             _r('no-http', 'HTTP management disabled', 'high', 'must_match', r'disable-http\s+yes', 'Disable HTTP on the management interface.'),
             _r('ntp', 'NTP servers configured', 'medium', 'must_match', r'ntp-servers', 'Configure primary/secondary NTP servers.'),
             _r('syslog', 'Syslog server profile configured', 'medium', 'must_match', r'\bsyslog\s*\{', 'Configure a syslog server profile and log forwarding.'),
             _r('login-banner', 'Login banner configured', 'low', 'must_match', r'login-banner', 'Configure a login banner.'),
         ]),
    dict(key='junos-hardening', version=1, platforms=['juniper_junos'], name='Juniper Junos hardening',
         description='Management-plane hardening checks for Junos set-format configuration (starter pack).',
         rules=[
             _r('root-ssh', 'Root SSH login denied', 'high', 'must_match', r'^set system services ssh root-login deny\b', 'Set "system services ssh root-login deny".'),
             _r('no-telnet', 'Telnet service disabled', 'high', 'must_not_match', r'^set system services telnet\b', 'Delete "system services telnet".'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^set system ntp server\s', 'Configure "system ntp server".'),
             _r('syslog', 'Remote syslog configured', 'medium', 'must_match', r'^set system syslog host\s', 'Configure "system syslog host".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match', r'^set snmp community (?:public|private)\b',
                'Remove default communities; prefer SNMPv3.'),
         ]),
    dict(key='comware-vrp-hardening', version=1, platforms=['hp_comware', 'huawei'], name='HPE Comware / Huawei VRP hardening',
         description='Management-plane hardening checks for Comware and VRP (starter pack).',
         rules=[
             _r('no-telnet', 'Telnet server disabled', 'high', 'must_not_match', r'^\s*telnet server enable\b', 'Undo "telnet server enable".'),
             _r('ntp', 'NTP server configured', 'medium', 'must_match', r'^\s*ntp-service (?:unicast-server|server)\s', 'Configure "ntp-service unicast-server".'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^\s*info-center loghost\s', 'Configure "info-center loghost".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match',
                r'^\s*snmp-agent community (?:read|write)\s+(?:simple\s+|cipher\s+)?(?:public|private)\b', 'Remove default communities; prefer SNMPv3.'),
         ]),
    dict(key='aruba-hardening', version=1, platforms=['aruba_aoscx', 'aruba_osswitch', 'aruba_os'], name='Aruba switching hardening',
         description='Management-plane hardening checks for AOS-CX, ArubaOS-Switch and ArubaOS (starter pack).',
         rules=[
             _r('ntp', 'Time synchronization configured', 'medium', 'must_match', r'^\s*(?:ntp server|sntp server|timesync)\b',
                'Configure NTP/SNTP servers.'),
             _r('remote-logging', 'Remote syslog configured', 'medium', 'must_match', r'^\s*logging\s+(?:\d{1,3}(?:\.\d{1,3}){3}|[\w.-]+\s+vrf)\b',
                'Configure "logging <collector>".'),
             _r('snmp-default-community', 'No default SNMP communities', 'critical', 'must_not_match',
                r'^\s*snmp-server community "?(?:public|private)"?', 'Remove default communities; prefer SNMPv3.'),
             _r('no-telnet', 'Telnet server disabled', 'high', 'must_not_match', r'^\s*telnet-server\b(?!.*\bdisable)',
                'Disable the Telnet server ("no telnet-server").'),
         ]),
]


async def ensure_builtin(db):
    """Upsert built-in packs. Rule rows are keyed by (policy, rule_key) so
    results and exceptions survive pack upgrades; the operator's
    enabled/disabled choice is preserved."""
    for pack in BUILTIN_POLICIES:
        row = (await db.execute(text('SELECT id,version FROM ncm_policies WHERE builtin_key=:k'), {'k': pack['key']})).first()
        if row and row.version >= pack['version']:
            continue
        if row:
            policy_id = row.id
            await db.execute(text('''UPDATE ncm_policies SET name=:n,description=:d,version=:v,platforms=:p,updated_at=now() WHERE id=:id'''),
                             {'id': policy_id, 'n': pack['name'], 'd': pack['description'], 'v': pack['version'], 'p': pack['platforms']})
        else:
            policy_id = (await db.execute(text('''INSERT INTO ncm_policies(builtin_key,name,description,version,platforms,created_by)
                VALUES(:k,:n,:d,:v,:p,'built-in') ON CONFLICT DO NOTHING RETURNING id'''),
                {'k': pack['key'], 'n': pack['name'], 'd': pack['description'], 'v': pack['version'], 'p': pack['platforms']})).scalar()
            if policy_id is None:
                continue
        keys = []
        for position, rule in enumerate(pack['rules']):
            keys.append(rule['rule_key'])
            await db.execute(text('''INSERT INTO ncm_policy_rules(policy_id,rule_key,title,description,severity,check_type,pattern,section_pattern,remediation,position)
                VALUES(:p,:k,:t,:d,:s,:c,:pat,:sec,:rem,:pos)
                ON CONFLICT(policy_id,rule_key) DO UPDATE SET title=:t,description=:d,severity=:s,check_type=:c,pattern=:pat,
                  section_pattern=:sec,remediation=:rem,position=:pos'''),
                {'p': policy_id, 'k': rule['rule_key'], 't': rule['title'], 'd': rule['description'], 's': rule['severity'],
                 'c': rule['check_type'], 'pat': rule['pattern'], 'sec': rule['section_pattern'], 'rem': rule['remediation'], 'pos': position})
        await db.execute(text('DELETE FROM ncm_policy_rules WHERE policy_id=:p AND NOT (rule_key = ANY(:keys))'), {'p': policy_id, 'keys': keys})


def _indent(line):
    return len(line) - len(line.lstrip(' \t'))


def sections(lines, header):
    """Yield (index, header line, [(index, line), ...]) for each section."""
    for i, line in enumerate(lines):
        if not header.search(line):
            continue
        base = _indent(line)
        body = []
        for j in range(i + 1, len(lines)):
            current = lines[j]
            if not current.strip():
                continue
            if _indent(current) <= base:
                break
            body.append((j, current))
        yield i, line, body


def _evidence(items, limit=5):
    return [{'line': n + 1, 'text': redact_config(t.rstrip())[:300]} for n, t in items[:limit]]


def check(rule, content):
    """Evaluate one rule. Returns (status, detail, evidence)."""
    pattern = safe_pattern(rule['pattern'])
    lines = content.splitlines()
    kind = rule['check_type']
    if kind in ('must_match', 'must_not_match'):
        hits = [(n, line) for n, line in enumerate(lines) if pattern.search(line)]
        if kind == 'must_match':
            return ('pass', f'{len(hits)} matching line(s) found.', _evidence(hits, 3)) if hits else ('fail', 'Required configuration not found.', [])
        return ('fail', f'{len(hits)} prohibited line(s) found.', _evidence(hits)) if hits else ('pass', 'No prohibited configuration found.', [])
    header = safe_pattern(rule.get('section_pattern') or '')
    found = list(sections(lines, header))
    if not found:
        if kind == 'section_must_not_match':
            return 'pass', 'No matching sections exist.', []
        return 'fail', 'No matching configuration section exists.', []
    with_hit = [(i, h, [(n, l) for n, l in body if pattern.search(l)]) for i, h, body in found]
    if kind == 'section_must_match':
        missing = [(i, h) for i, h, hits in with_hit if not hits]
        if missing:
            return 'fail', f'{len(missing)} of {len(found)} section(s) lack the required setting.', _evidence(missing)
        return 'pass', f'All {len(found)} section(s) contain the required setting.', []
    if kind == 'section_must_not_match':
        bad = [hit for _, _, hits in with_hit for hit in hits]
        if bad:
            return 'fail', f'{len(bad)} prohibited line(s) inside matching sections.', _evidence(bad)
        return 'pass', f'None of {len(found)} section(s) contain the prohibited setting.', []
    if any(hits for _, _, hits in with_hit):
        return 'pass', 'At least one matching section contains the setting.', []
    return 'fail', f'None of {len(found)} matching section(s) contain the setting.', _evidence([(i, h) for i, h, _ in with_hit])


def _applies(platforms, platform):
    return not platforms or (platform in platforms)


async def _rules(db):
    return (await db.execute(text('''SELECT r.*,p.platforms AS policy_platforms FROM ncm_policy_rules r
        JOIN ncm_policies p ON p.id=r.policy_id WHERE p.enabled AND r.enabled ORDER BY p.name,r.position'''))).mappings().all()


async def _exceptions(db):
    rows = (await db.execute(text('''SELECT id,rule_id,device_id FROM ncm_compliance_exceptions
        WHERE revoked_at IS NULL AND expires_at>now()'''))).fetchall()
    return {(r.rule_id, r.device_id): r.id for r in rows}


async def evaluate_device(db, device_id, *, version_id=None, content=None, platform=None, rules=None, exceptions=None):
    """Evaluate every applicable rule for one device and store the outcomes."""
    await ensure_builtin(db)
    if content is None:
        row = (await db.execute(text('''SELECT c.id,c.content,c.content_enc,c.content_hash,c.platform,n.platform AS enrolled_platform
            FROM device_ncm n LEFT JOIN LATERAL (SELECT * FROM device_configs c WHERE c.device_id=n.device_id
              AND c.config_type='running' AND c.validated ORDER BY c.captured_at DESC,c.id DESC LIMIT 1) c ON TRUE
            WHERE n.device_id=:d'''), {'d': device_id})).first()
        if not row:
            return None
        platform = row.platform or (row.enrolled_platform if row.enrolled_platform != 'autodetect' else None)
        if row.id is not None:
            version_id = row.id
            try:
                content = read_content(row)
            except Exception:
                content = None
    rules = rules if rules is not None else await _rules(db)
    exceptions = exceptions if exceptions is not None else await _exceptions(db)
    counts = {'pass': 0, 'fail': 0, 'unknown': 0, 'excepted': 0}
    evaluated = []
    for rule in rules:
        if not platform or not _applies(rule['policy_platforms'], platform) or not _applies(rule['platforms'], platform):
            continue
        if content is None:
            status, detail, evidence = 'unknown', 'No validated running configuration is available to assess.', []
        else:
            try:
                status, detail, evidence = check(rule, content)
            except ValueError as exc:
                status, detail, evidence = 'unknown', f'Rule cannot be evaluated: {exc}', []
        exception_id = exceptions.get((rule['id'], device_id)) or exceptions.get((rule['id'], None))
        if exception_id and status in ('fail', 'unknown'):
            detail = f'Excepted ({status} before exception): {detail}'
            status = 'excepted'
        counts[status] += 1
        evaluated.append(rule['id'])
        await db.execute(text('''INSERT INTO ncm_compliance_results(device_id,rule_id,policy_id,version_id,status,evidence,detail,exception_id,assessed_at)
            VALUES(:d,:r,:p,:v,:s,CAST(:e AS jsonb),:detail,:x,now())
            ON CONFLICT(device_id,rule_id) DO UPDATE SET policy_id=:p,version_id=:v,status=:s,evidence=CAST(:e AS jsonb),
              detail=:detail,exception_id=:x,assessed_at=now()'''),
            {'d': device_id, 'r': rule['id'], 'p': rule['policy_id'], 'v': version_id, 's': status,
             'e': json.dumps(evidence), 'detail': detail, 'x': exception_id})
    # Rules that no longer apply (policy disabled, platform changed) drop out.
    await db.execute(text('DELETE FROM ncm_compliance_results WHERE device_id=:d AND NOT (rule_id = ANY(:ids))'),
                     {'d': device_id, 'ids': evaluated})
    return counts


async def evaluate_all(db, device_ids=None):
    await ensure_builtin(db)
    rules = await _rules(db)
    exceptions = await _exceptions(db)
    ids = device_ids if device_ids is not None else [r.device_id for r in (await db.execute(text('SELECT device_id FROM device_ncm'))).fetchall()]
    totals = {'devices': 0, 'pass': 0, 'fail': 0, 'unknown': 0, 'excepted': 0}
    for device_id in ids:
        counts = await evaluate_device(db, device_id, rules=rules, exceptions=exceptions)
        if counts is None:
            continue
        totals['devices'] += 1
        for k, v in counts.items():
            totals[k] += v
    await snapshot(db)
    return totals


async def snapshot(db, day=None):
    day = day or datetime.now(timezone.utc).date()
    await db.execute(text('''INSERT INTO ncm_compliance_snapshots(day,policy_id,devices,passed,failed,unknown,excepted,compliant_devices,taken_at)
        SELECT :day,r.policy_id,count(DISTINCT r.device_id),
          count(*) FILTER (WHERE r.status='pass'),count(*) FILTER (WHERE r.status='fail'),
          count(*) FILTER (WHERE r.status='unknown'),count(*) FILTER (WHERE r.status='excepted'),
          (SELECT count(*) FROM (SELECT device_id FROM ncm_compliance_results x WHERE x.policy_id=r.policy_id
             GROUP BY device_id HAVING bool_and(x.status IN ('pass','excepted'))) c),now()
        FROM ncm_compliance_results r GROUP BY r.policy_id
        ON CONFLICT(day,policy_id) DO UPDATE SET devices=EXCLUDED.devices,passed=EXCLUDED.passed,failed=EXCLUDED.failed,
          unknown=EXCLUDED.unknown,excepted=EXCLUDED.excepted,compliant_devices=EXCLUDED.compliant_devices,taken_at=now()'''),
        {'day': day})


async def daily(db):
    """Run from the NCCM scheduler: at most one full evaluation per UTC day
    (captures are evaluated as they arrive), so exception expiry and policy
    edits are reflected and trends get one point per day."""
    from app.services import ncm_events
    done = (await db.execute(text('''SELECT 1 FROM ncm_audit WHERE action='compliance.daily'
        AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' LIMIT 1'''))).first()
    if done:
        return None
    totals = await evaluate_all(db)
    await ncm_events.audit(db, None, 'compliance.daily', metadata=totals)
    return totals
