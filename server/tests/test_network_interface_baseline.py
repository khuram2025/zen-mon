"""Interface-down only for ports that have been up (ZP-15), and the device IP
in every interface/metric alert (ZP-14).

A freshly added 48-port switch raised one Critical "Interface down" per unused
access port on the first evaluation: admin-up/oper-down is the normal state of
a port nothing is plugged into. device_interfaces.last_up_at (migrate-126) is
the "ever up" baseline.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services import network_alert_service as worker
from app.services.network_history import entities

OPER_DOWN = [{'metric': 'if_oper_status', 'operator': '==', 'threshold': 2}]


def iface(if_index=1, *, admin_up=True, ever_up=True):
    return {'if_index': if_index, 'if_name': f'Gi1/0/{if_index}', 'if_descr': '',
            'if_alias': '', 'speed': 0, 'admin_up': admin_up, 'ever_up': ever_up}


def bound(ifaces):
    series = {i['if_index']: {'if_oper_status': [(100.0, 2)]} for i in ifaces}
    return {identity: hist for identity, _label, hist, _meta
            in entities(OPER_DOWN, {}, series, ifaces, lambda _i: True)}


def test_never_up_port_has_no_oper_status_evidence():
    out = bound([iface(1, ever_up=False), iface(2, ever_up=True)])
    assert 'if_oper_status' not in out[1]
    assert out[2]['if_oper_status'] == [(100.0, 2)]


def test_admin_down_port_is_still_excluded():
    assert 'if_oper_status' not in bound([iface(1, admin_up=False)])[1]


def test_legacy_interface_dicts_without_the_flag_stay_eligible():
    legacy = iface(1)
    del legacy['ever_up']
    assert 'if_oper_status' in bound([legacy])[1]


def test_legacy_evaluator_skips_never_up_ports():
    rule = NS(metric='if_oper_status', operator='eq', threshold=2)
    assert worker._eval_interface(rule, {'oper': 2}, iface(ever_up=False)) is None
    assert worker._eval_interface(rule, {'oper': 2}, iface())[0] is True


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class IfaceDB:
    def __init__(self, fail_first=False):
        self.fail_first = fail_first
        self.sql = []
        self.rolled_back = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if self.fail_first and 'last_up_at' in sql:
            raise Exception('column "last_up_at" does not exist')
        ever_up = 'last_up_at' not in sql  # baseline query: port never up
        return _Rows([('d1', 1, 'Gi1/0/1', '', '', 1000, 'up', True, ever_up)])

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_interfaces_reads_the_last_up_baseline():
    db = IfaceDB()
    out = await worker._interfaces(db)
    assert 'last_up_at IS NOT NULL' in db.sql[0]
    assert out['d1'][0]['ever_up'] is False


@pytest.mark.asyncio
async def test_interfaces_degrades_before_migration_126():
    db = IfaceDB(fail_first=True)
    out = await worker._interfaces(db)
    assert db.rolled_back
    assert out['d1'][0]['ever_up'] is True


# ── ZP-14: the IP travels with the hostname ─────────────────────────────────

def _rule(**overrides):
    data = dict(id='r1', name='Interface down', metric='if_oper_status', operator='eq',
                threshold=2, severity='critical', notify_channels=['c'], cooldown=0,
                conditions=None, condition_logic='AND', recovery_alert=True,
                schedule_start=None, schedule_end=None, schedule_days=None,
                email_subject=None, email_body=None, sms_template=None,
                recovery_email_subject=None, recovery_email_body=None,
                recovery_sms_template=None)
    data.update(overrides)
    return NS(**data)


def test_stored_message_names_host_and_ip():
    assert worker._message(_rule(), 'core-sw', 'if_oper_status=2', 'Gi1/0/1', '10.0.0.1') == (
        'Interface down: core-sw (10.0.0.1) / Gi1/0/1 — if_oper_status=2')
    # Unknown IP: no empty parentheses.
    assert worker._message(_rule(), 'core-sw', 'x', None) == 'Interface down: core-sw — x'


@pytest.mark.asyncio
async def test_notification_carries_the_ip(monkeypatch):
    from app.services import alert_schedule
    monkeypatch.setattr(alert_schedule, 'get_configured_timezone', AsyncMock(return_value='UTC'))
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *a: True)
    sink = AsyncMock(return_value=1)
    monkeypatch.setattr(worker, 'dispatch_to_channels', sink)

    assert await worker._notify(None, _rule(), 'core-sw', iface_label='Gi1/0/1',
                                ip_address='10.0.0.1') is True
    ctx = sink.call_args.args[2]
    assert ctx['ip_address'] == '10.0.0.1'
    assert ctx['subject'] == '[CRITICAL] ALERT: Interface down — core-sw (10.0.0.1) / Gi1/0/1'
    assert 'core-sw (10.0.0.1) / Gi1/0/1' in ctx['message']

    await worker._notify(None, _rule(), 'core-sw', iface_label='Gi1/0/1',
                         ip_address='10.0.0.1', is_recovery=True, duration='5 minutes')
    ctx = sink.call_args.args[2]
    assert ctx['subject'] == '[CRITICAL] Resolved: Interface down — core-sw (10.0.0.1) / Gi1/0/1'


@pytest.mark.asyncio
async def test_apply_forwards_the_ip_to_message_and_notification(monkeypatch):
    monkeypatch.setattr(worker, '_active_alert', AsyncMock(return_value=None))
    raise_ = AsyncMock(return_value='a1')
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, '_raise', raise_)
    monkeypatch.setattr(worker, '_notify', notify)
    monkeypatch.setattr(worker.ns, 'stamp', AsyncMock())
    db = NS(execute=AsyncMock(), commit=AsyncMock())

    await worker._apply(db, _rule(), 'd1', 'core-sw', 1, 'Gi1/0/1', True, 2.0,
                        'if_oper_status=2', {}, 0, 0, set(), ip_address='10.0.0.1')
    assert raise_.call_args.args[3] == 'Interface down: core-sw (10.0.0.1) / Gi1/0/1 — if_oper_status=2'
    assert notify.call_args.kwargs['ip_address'] == '10.0.0.1'
