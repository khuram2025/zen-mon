from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from paramiko import HostKeys, RSAKey, AuthenticationException, SSHException
from app.services import ncm_readiness as readiness
from app.services.ncm_errors import failure_message
from app.services.ncm_status import backup_status


def target(**changes):
    return dict(device_id='fixture', eligible=True, eligibility_reason='fixture', vendor='Fortinet',
                platform='fortinet', config_types=['running'], profile_configured=True,
                ncm_enabled=True, fresh=False, schedule_enabled=False, **changes)


def test_readiness_does_not_confuse_prerequisites_with_capture():
    result=readiness.device_readiness(target(),dict(worker='active',scheduler='active',delivery='active',encryption='configured'))
    assert result['can_capture'] and not result['capture_verified']
    assert next(c for c in result['checks'] if c['id']=='capture')['status']=='pending'


@pytest.mark.parametrize('runtime',[
    dict(worker='inactive',scheduler='active',delivery='active',encryption='configured'),
    dict(worker='active',scheduler='active',delivery='active',encryption='missing'),
])
def test_missing_required_prerequisite_blocks_attempt(runtime):
    assert not readiness.device_readiness(target(),runtime)['can_capture']


def test_driver_is_separate_from_eligibility():
    assert readiness.support_details('Aruba',None)['state']=='needs_detection'
    assert readiness.support_details('Aruba','aruba_aoscx')['state']=='implemented'
    assert readiness.support_details('F5','autodetect')['state']=='unsupported'
    assert readiness.support_details('Fortinet','fortinet',['startup'])['state']=='unsupported'
    assert readiness.support_details('Fortinet','fortinet')['state']=='implemented'
    assert readiness.support_details('Cisco',None)['state']=='needs_detection'


def test_readiness_has_no_ssh_trust_prerequisite():
    result=readiness.device_readiness(target(),dict(worker='active',scheduler='active',delivery='active',encryption='configured'))
    assert 'trust' not in [c['id'] for c in result['checks']]


@pytest.mark.parametrize('stamp',['invalid','2026-01-01T12:00:00','2099-01-01T00:00:00+00:00'])
def test_invalid_or_future_evidence_is_unknown(stamp):
    row=SimpleNamespace(ip='192.0.2.1',device_type='other',vendor='F5',eligible_override=None,
        config_types=['running'],verified_types={'running':stamp},ncm_enabled=True,
        freshness_hours=24,platform='autodetect',last_status=None,validated_at=None,versions=1)
    status=backup_status(row,datetime(2026,9,22,tzinfo=timezone.utc))
    assert not status['fresh'] and not status['eligible'] and status['eligibility_review']
    row.eligible_override=True
    assert backup_status(row)['eligible'] and not backup_status(row)['eligibility_review']


@pytest.mark.parametrize('exc,stage,expected',[
    (AuthenticationException('SECRET'),'collection','Authentication rejected'),
    (SSHException('SECRET'),'collection','negotiation failed'),
    (TimeoutError('SECRET'),'collection','timed out'),
    (RuntimeError('SECRET'),'archive','Archive failed'),
    (RuntimeError('SECRET'),'credentials','Credentials unavailable'),
    (RuntimeError('SECRET'),'collection','Collection failed'),
])
def test_failures_are_actionable_without_exception_disclosure(exc,stage,expected):
    message=failure_message(exc,stage)
    assert expected in message and 'SECRET' not in message
