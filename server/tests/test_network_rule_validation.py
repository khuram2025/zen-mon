import pytest
from fastapi import HTTPException
from app.api.v1.alert_rules import _validate_network_contract
from app.services.network_conditions import UNMAPPED_METRICS


@pytest.mark.parametrize('metric', sorted(UNMAPPED_METRICS))
def test_unsupported_state_keys_cannot_be_silently_saved(metric):
    with pytest.raises(HTTPException) as exc:
        _validate_network_contract(metric, None)
    assert exc.value.status_code == 422


def test_cross_engine_conditions_fail_authoring():
    with pytest.raises(HTTPException):
        _validate_network_contract('cpu', [dict(metric=m, operator='>', threshold=10)
                                          for m in ['cpu', 'packet_loss']])


def test_collected_templates_and_legacy_ping_remain_valid():
    _validate_network_contract('tpl_fgt_tunnel_state', None)
    _validate_network_contract('ping_status', None)


def test_partial_update_preserves_and_validates_compound_contract():
    from app.api.v1.alert_rules import _merge_condition_update
    current = dict(metric='cpu', threshold=90, conditions=[
        dict(metric='cpu', operator='>', threshold=90, reset_threshold=80),
        dict(metric='memory', operator='>', threshold=95)])
    updated = _merge_condition_update(current, {'threshold': 92})
    assert updated['conditions'][0]['threshold'] == 92
    assert updated['conditions'][1]['metric'] == 'memory'
    with pytest.raises(HTTPException):
        _merge_condition_update(current, {'threshold': 70})
    with pytest.raises(HTTPException):
        _merge_condition_update(current, {'metric': 'packet_loss'})


def test_syslog_severity_and_reset_contract():
    from app.api.v1.alert_rules import _merge_condition_update
    for threshold in [-1, 7.5, 8, float('inf')]:
        with pytest.raises(HTTPException):
            _merge_condition_update({'metric': 'syslog'}, {'threshold': threshold})
    assert _merge_condition_update({'metric': 'syslog'}, {'threshold': 4})['recovery_alert'] is False
