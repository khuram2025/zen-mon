"""Access link churn is diagnostic; infrastructure outages retain coverage."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import network_alert_service as worker
from app.services.network_history import entities
from app.services.network_conditions import evaluate

TARGET = 'role:infrastructure'
CONDITION = [{'metric': 'if_oper_status', 'operator': 'eq', 'threshold': 2}]

def port(idx=1, **kw):
    return dict(if_index=idx, if_name=f'Gi1/0/{idx}', if_descr='', if_alias='',
                speed=1000000000, admin_up=True, ever_up=True, **kw)

@pytest.mark.parametrize('role,expected', [(False, False), (True, True), (None, True)])
def test_infrastructure_policy_preserves_unknown_and_uplink_coverage(role, expected):
    assert worker._iface_matches_target(port(is_uplink=role), TARGET) is expected

def test_critical_endpoint_can_opt_in_without_being_an_uplink():
    assert worker._iface_matches_target(port(is_uplink=False, tags={'link-critical'}), TARGET)

def test_custom_rule_can_still_monitor_access_port():
    p = port(is_uplink=False)
    assert worker._iface_matches_target(p, None)
    assert worker._iface_matches_target(p, 'Gi1/0/1')
    assert worker._iface_matches_target(p, '1')

def test_access_port_is_not_bound_but_uplink_has_sustained_down_evidence():
    ifaces = [port(1, is_uplink=False), port(2, is_uplink=True), port(3, is_uplink=None)]
    series = {i: {'if_oper_status': [(100, 2), (190, 2), (280, 2)]} for i in (1, 2, 3)}
    bound = list(entities(CONDITION, {}, series, ifaces,
                         lambda i: worker._iface_matches_target(i, TARGET)))
    assert [b[0] for b in bound] == [2, 3]
    assert evaluate(CONDITION, 'AND', bound[0][2], now=280, hold=180, max_gap=180).breach is True
    assert evaluate(CONDITION, 'AND', {'if_oper_status': [(190, 2), (280, 2)]},
                    now=280, hold=180, max_gap=180).breach is None

@pytest.mark.asyncio
async def test_policy_closure_targets_only_excluded_ports_and_is_not_recovery():
    db = NS(execute=AsyncMock(return_value=NS(rowcount=1)))
    result = await worker._retire_access_alerts(db, NS(target=TARGET, id='r'), 'd',
        [port(1, is_uplink=False), port(2, is_uplink=True), port(3, is_uplink=None),
         port(4, is_uplink=False, tags={'link-critical'})])
    assert result == 1
    params = db.execute.call_args.args[1]
    assert params['indices'] == ['1']
    assert 'policy_scope_change' in params['meta']
    assert '"is_recovery": false' in params['meta']

@pytest.mark.asyncio
async def test_custom_rule_never_retires_access_incidents():
    db = NS(execute=AsyncMock())
    assert await worker._retire_access_alerts(db, NS(target=None), 'd', [port(is_uplink=False)]) == 0
    db.execute.assert_not_awaited()

@pytest.mark.asyncio
async def test_loaded_roles_honor_manual_override_and_tags():
    class Rows:
        def __init__(self, rows): self.rows = rows
        def all(self): return self.rows
    async def execute(stmt, params=None):
        sql = str(stmt)
        if sql.startswith('INSERT'):
            assert 'ON CONFLICT (device_id, if_name) DO NOTHING' in sql
            return Rows([])
        if 'LEFT JOIN interface_alert_infrastructure' in sql:
            # Port 1's retained positive evidence survives current FDB loss;
            # manual access still wins on port 2; manual uplink wins on port 3.
            return Rows([('d', 1, True, None), ('d', 2, True, 'access'),
                         ('d', 3, False, 'uplink'), ('d', 4, False, None)])
        return Rows([('d', i, f'Gi1/0/{i}', '', '', 1000, 'up', True, True,
                      ['Link-Critical'] if i == 4 else []) for i in (1, 2, 3, 4)])
    loaded = (await worker._interfaces(NS(execute=execute)))['d']
    assert [worker._iface_matches_target(p, TARGET) for p in loaded] == [True, False, True, True]
