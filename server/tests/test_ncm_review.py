"""Unit contracts for the NCCM development-review fixes (no database)."""
from datetime import datetime, time, timezone, timedelta
import socket

import pytest
from paramiko import AuthenticationException, BadHostKeyException, SSHException
from paramiko.ssh_exception import NoValidConnectionsError

from app.services import ncm_schedule as schedule
from app.services import ncm_compliance as compliance
from app.services import ncm_changes as changes
from app.services import ncm_collection as collection
from app.services import ncm_readiness as readiness
from app.services.ncm_content import safe_pattern
from app.services.ncm_errors import classify, PrerequisiteError

UTC = timezone.utc


def s(**kw):
    base = dict(schedule_enabled=True, schedule_type='interval', schedule_interval_hours=24,
                schedule_time=None, schedule_days=None, schedule_timezone='UTC', last_scheduled_at=None)
    base.update(kw)
    return base


# --- B01/B07: one due calculation for scheduler and preview -------------------
def test_interval_schedule():
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert schedule.is_due(s(), now)
    assert not schedule.is_due(s(last_scheduled_at=now - timedelta(hours=3)), now)
    assert schedule.next_due(s(last_scheduled_at=now - timedelta(hours=3)), now) == now + timedelta(hours=21)
    assert schedule.is_due(s(last_scheduled_at=now - timedelta(hours=25)), now)
    assert schedule.next_due(s(schedule_enabled=False), now) is None


def test_daily_schedule_uses_device_timezone():
    # 02:00 Asia/Riyadh (UTC+3) is 23:00 UTC the previous day.
    riyadh = s(schedule_type='daily', schedule_time=time(2, 0), schedule_timezone='Asia/Riyadh')
    utc = s(schedule_type='daily', schedule_time=time(2, 0), schedule_timezone='UTC')
    now = datetime(2026, 9, 30, 0, 30, tzinfo=UTC)          # 03:30 Riyadh, 00:30 UTC
    assert schedule.is_due(riyadh, now)
    assert not schedule.is_due(utc, now)
    assert schedule.next_due(utc, now) == datetime(2026, 9, 30, 2, 0, tzinfo=UTC)
    ran = dict(riyadh, last_scheduled_at=datetime(2026, 9, 29, 23, 0, 5, tzinfo=UTC))
    assert not schedule.is_due(ran, now)
    assert schedule.next_due(ran, now) == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)


def test_new_schedule_does_not_fire_for_a_past_slot():
    daily = s(schedule_type='daily', schedule_time=time(2, 0))
    assert not schedule.is_due(daily, datetime(2026, 9, 30, 1, 0, tzinfo=UTC))
    assert schedule.is_due(daily, datetime(2026, 9, 30, 2, 1, tzinfo=UTC))


def test_weekly_schedule_days():
    weekly = s(schedule_type='weekly', schedule_time='06:00', schedule_days=[1, 3])  # Mon, Wed
    wed = datetime(2026, 9, 30, 7, 0, tzinfo=UTC)  # Wednesday
    assert schedule.is_due(weekly, wed)
    done = dict(weekly, last_scheduled_at=datetime(2026, 9, 30, 6, 0, 30, tzinfo=UTC))
    assert schedule.next_due(done, wed) == datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # next Monday
    assert schedule.next_due(s(schedule_type='weekly', schedule_time='06:00', schedule_days=[]), wed) is None


def test_dst_transition_schedules_once():
    ny = s(schedule_type='daily', schedule_time='02:30', schedule_timezone='America/New_York')
    # 2026-03-08 02:30 does not exist in New York; the run still happens once that day.
    now = datetime(2026, 3, 8, 12, tzinfo=UTC)
    due = schedule.next_due(ny, now)
    assert due is not None and due.date() == now.date() and due <= now
    ran = dict(ny, last_scheduled_at=due + timedelta(seconds=5))
    assert not schedule.is_due(ran, now)


def test_backward_clock_correction_does_not_stall_schedule():
    now = datetime(2026, 9, 30, 10, tzinfo=UTC)
    future = s(last_scheduled_at=now + timedelta(hours=12))
    assert schedule.clock_anomaly(future['last_scheduled_at'], now)
    assert schedule.is_due(future, now)


def test_schedule_summary_distinguishes_modes():
    assert schedule.describe(s()) == 'Every 24 h'
    assert schedule.describe(s(schedule_type='daily', schedule_time='02:00', schedule_timezone='Asia/Riyadh')) == 'Daily at 02:00 Asia/Riyadh'
    assert schedule.describe(s(schedule_type='weekly', schedule_time='02:00', schedule_days=[1, 5])) == 'Weekly Mon, Fri at 02:00 UTC'
    assert schedule.describe(s(schedule_enabled=False)) == 'Not scheduled'


# --- B06: retryable versus permanent failures ---------------------------------
@pytest.mark.parametrize('exc,category,retryable', [
    (AuthenticationException('x'), 'authentication', False),
    (BadHostKeyException('h', type('K', (), {'get_base64': lambda s: 'a', 'get_name': lambda s: 'ssh-ed25519'})(),
                         type('K', (), {'get_base64': lambda s: 'b', 'get_name': lambda s: 'ssh-ed25519'})()), 'host_key_changed', False),
    (SSHException('Server x not found in known_hosts'), 'host_key_untrusted', False),
    (NoValidConnectionsError({('192.0.2.1', 22): OSError('x')}), 'unreachable', True),
    (socket.timeout('x'), 'timeout', True),
    (collection.CaptureError('Device rejected', 'privilege'), 'privilege', False),
    (collection.CaptureError('Incomplete capture'), 'incomplete', True),
    (collection.CaptureError('unsupported', 'driver'), 'driver', False),
    (PrerequisiteError('no trust', 'host_key_untrusted'), 'host_key_untrusted', False),
])
def test_failure_classification(exc, category, retryable):
    failure = classify(exc)
    assert failure.category == category and failure.retryable is retryable
    assert 'x' != failure.message


def test_credential_and_archive_stage_failures():
    assert classify(RuntimeError('SECRET'), 'credentials').retryable is False
    assert classify(RuntimeError('SECRET'), 'archive').retryable is True
    assert 'SECRET' not in classify(RuntimeError('SECRET'), 'archive').message


# --- B02: readiness enforcement semantics ------------------------------------
RUNTIME = dict(worker='active', scheduler='active', delivery='active', encryption='configured')


def target(**changes_):
    return {**dict(device_id='fixture', eligible=True, eligibility_reason='fixture', vendor='Cisco', platform='cisco_ios',
                   config_types=['running'], profile_configured=True, ncm_enabled=True, fresh=False,
                   schedule_enabled=False), **changes_}


def test_eligibility_controls_coverage_not_collection():
    result = readiness.device_readiness(target(eligible=False), RUNTIME, dict(state='pass', message='ok'))
    assert result['can_capture']
    assert next(c for c in result['checks'] if c['id'] == 'eligibility')['status'] == 'info'


def test_worker_state_unknown_is_not_a_blocker_but_inactive_is():
    unknown = readiness.device_readiness(target(), dict(RUNTIME, worker='unknown'), dict(state='pass', message='ok'))
    inactive = readiness.device_readiness(target(), dict(RUNTIME, worker='inactive'), dict(state='pass', message='ok'))
    assert unknown['can_capture'] and not inactive['can_capture']
    assert [b['id'] for b in inactive['blockers']] == ['worker']


def test_blockers_are_structured():
    result = readiness.device_readiness(target(), RUNTIME, dict(state='blocked', message='no trust'))
    assert result['blockers'] == [{'id': 'trust', 'label': 'SSH host trust', 'detail': 'no trust'}]
    assert next(c for c in result['checks'] if c['id'] == 'trust')['action'] == 'trust'


# --- F02/B22: driver capability metadata --------------------------------------
def test_platform_catalog_exposes_capability():
    catalog = {p['value']: p for p in collection.platform_catalog()}
    assert catalog['fortinet']['config_types'] == ['running']
    assert catalog['paloalto_panos']['config_types'] == ['running']
    assert catalog['cisco_ios']['config_types'] == ['running', 'startup']
    assert not catalog['autodetect']['capability_resolved']
    assert {'aruba_aoscx', 'aruba_osswitch', 'aruba_os'} <= set(catalog)
    assert 'cisco_xe' not in catalog  # alias, not a separate choice
    assert readiness.support_details('F5', None)['state'] == 'unsupported'


@pytest.mark.parametrize('platform,config', [
    ('aruba_aoscx', 'Current configuration:\n!\n!Version ArubaOS-CX GL.10.10.1010\n!export-password: default\nhostname core1\nvlan 1\ninterface 1/1/1\n    no shutdown\n'),
    ('aruba_osswitch', 'Running configuration:\n\n; J9729A Configuration Editor; Created on release #WB.16.10.0009\n; Ver #14\nhostname "edge-sw"\nvlan 1\n   name "DEFAULT_VLAN"\n   exit\n'),
    ('aruba_os', 'version 8.10\nhostname "mc1"\ninterface gigabitethernet 0/0/0\n'),
    ('hp_comware', '#\n version 7.1.064, Release 0821P16\n#\n sysname HPE\n#\nreturn\n'),
])
def test_new_and_fixed_markers(platform, config):
    assert collection.validate_config(config, platform) == config


def test_aruba_output_without_markers_is_rejected():
    with pytest.raises(collection.CaptureError) as error:
        collection.validate_config('Invalid input: show running-confi\n', 'aruba_osswitch')
    assert error.value.category == 'privilege'


# --- F01: compliance checks ---------------------------------------------------
CISCO = """hostname edge
service password-encryption
enable secret 9 $9$abc
ip ssh version 2
snmp-server community public RO
line vty 0 4
 access-class MGMT in
 transport input ssh
line vty 5 15
 transport input telnet ssh
!
end
"""


def rule(check, pattern, section=None):
    return dict(check_type=check, pattern=pattern, section_pattern=section)


def test_compliance_check_types():
    assert compliance.check(rule('must_match', r'^ip ssh version 2\b'), CISCO)[0] == 'pass'
    status, detail, evidence = compliance.check(rule('must_not_match', r'^snmp-server community (?:public|private)\b'), CISCO)
    assert status == 'fail' and evidence[0]['line'] == 5
    # Evidence is redacted: the community string never leaves the device record.
    assert 'public' not in evidence[0]['text']
    status, detail, evidence = compliance.check(rule('section_must_not_match', r'^\s+transport input\b.*\btelnet\b', r'^line vty\b'), CISCO)
    assert status == 'fail' and evidence[0]['line'] == 10
    status, _, evidence = compliance.check(rule('section_must_match', r'^\s+access-class\s+\S+\s+in\b', r'^line vty\b'), CISCO)
    assert status == 'fail' and [e['line'] for e in evidence] == [9]
    assert compliance.check(rule('section_any_must_match', r'access-class', r'^line vty\b'), CISCO)[0] == 'pass'
    assert compliance.check(rule('section_must_not_match', 'x', r'^router bgp'), CISCO)[0] == 'pass'


def test_fortios_nested_sections():
    forti = 'config system ntp\n    set ntpsync enable\nend\nconfig system interface\n    edit "port1"\n        set allowaccess ping https ssh\n    next\n    edit "port2"\n        set allowaccess ping http telnet\n    next\nend\n'
    builtin = next(p for p in compliance.BUILTIN_POLICIES if p['key'] == 'fortios-hardening')
    rules = {r['rule_key']: r for r in builtin['rules']}
    status, _, evidence = compliance.check(rules['no-cleartext-admin'], forti)
    assert status == 'fail' and evidence[0]['line'] == 9
    assert compliance.check(rules['ntp-sync'], forti)[0] == 'pass'


def test_builtin_packs_are_valid():
    for pack in compliance.BUILTIN_POLICIES:
        for r in pack['rules']:
            safe_pattern(r['pattern'])
            if r['check_type'].startswith('section'):
                safe_pattern(r['section_pattern'])
            assert r['severity'] in compliance.SEVERITIES and r['check_type'] in compliance.CHECK_TYPES
        assert all(p in collection.DRIVERS for p in pack['platforms'])


@pytest.mark.parametrize('pattern', ['(a+)+', r'(\w+\s?)+$', r'(x)\1', ''])
def test_unsafe_patterns_rejected(pattern):
    with pytest.raises(ValueError):
        safe_pattern(pattern)


# --- F08: change evidence ------------------------------------------------------
@pytest.mark.parametrize('message,user', [
    ('%SYS-5-CONFIG_I: Configured from console by netops on vty0 (10.0.0.5)', 'netops'),
    ('date=2026-09-30 logid="0100044547" type="event" user="admin" ui="ssh(10.0.0.9)" action="Edit"', 'admin'),
    ("UI_COMMIT: User 'jdoe' requested 'commit' operation", 'jdoe'),
    ('%%10CFGMAN/5/CFGMAN_CFGCHANGED: -EventIndex=4 configuration changed', None),
])
def test_change_messages_and_explicit_attribution(message, user):
    summary, reported = changes.classify_syslog(message)
    assert summary and reported == user


def test_unrelated_messages_are_ignored():
    assert changes.classify_syslog('%LINK-3-UPDOWN: Interface Gi0/1, changed state to up') is None
    assert changes.change_trap('.1.3.6.1.4.1.9.9.43.2.0.1')
    assert changes.change_trap('1.3.6.1.6.3.1.1.5.3') is None


def test_changed_daily_schedule_starts_at_next_slot():
    now = datetime(2026, 9, 30, 12, 47, tzinfo=UTC)
    before = s(schedule_enabled=False)
    after = s(schedule_type='daily', schedule_time='02:00')
    anchor = schedule.reanchor(before, after, now)
    assert anchor == now
    assert schedule.next_due(dict(after, last_scheduled_at=anchor), now) == datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    # Unchanged schedules and interval schedules are not re-anchored.
    assert schedule.reanchor(after, dict(after), now) is None
    assert schedule.reanchor(before, s(), now) is None
