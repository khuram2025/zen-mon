from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import network_alert_service as worker
from app.services import network_history
from app.api.v1 import alert_engine


def rule(metric='cpu', **overrides):
    data = dict(id='rule1', name='Fixture', metric=metric, operator='>', threshold=90,
        min_duration=0, severity='warning', conditions=None, condition_logic='AND',
        device_id=None, group_id=None, device_type=None, location=None, scope_tag=None,
        target=None, recovery_alert=True, notify_channels=['local-sink'],
        schedule_start=None, schedule_end=None, schedule_days=None)
    data.update(overrides)
    return NS(**data)


class DB:
    def __init__(self, r): self.rule = r; self.statements = []
    async def execute(self, query, params=None):
        self.statements.append(str(query))
        return NS(all=lambda: [self.rule] if 'FROM alert_rules' in str(query) else [])
    async def commit(self): pass


async def run(monkeypatch, r, history, dependency=None):
    db = DB(r)
    monkeypatch.setattr(worker, '_snmp_devices', AsyncMock(return_value={'d': dict(
        hostname='fixture', device_type='switch', location='', group_id=None, tags=set(), poll_interval=60)}))
    monkeypatch.setattr(worker, '_interfaces', AsyncMock(return_value={}))
    monkeypatch.setattr(worker, '_active_alert', AsyncMock(return_value=None))
    monkeypatch.setattr(network_history, 'fetch_history', lambda *args: ({'d': history}, {}))
    monkeypatch.setattr(alert_engine, '_find_suppressing_dependency', AsyncMock(return_value=dependency))
    applied = []
    async def apply(*args):
        applied.append(args)
        return 0, 0
    monkeypatch.setattr(worker, '_apply', apply)
    await worker.evaluate_network_rules(db)
    return applied, db


@pytest.mark.asyncio
async def test_worker_uses_both_conditions_not_only_flat_metric(monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    r = rule(conditions=[dict(metric=m, operator='>', threshold=90) for m in ['cpu', 'memory']])
    applied, db = await run(monkeypatch, r, {'cpu': [(now, 99)], 'memory': [(now, 10)]})
    assert len(applied) == 1 and applied[0][6] is False
    assert any('pg_advisory_xact_lock' in s for s in db.statements)


@pytest.mark.asyncio
async def test_worker_keeps_template_components_independent(monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    applied, _ = await run(monkeypatch, rule('tpl_tunnel', operator='==', threshold=2),
                          {'tpl_tunnel_1': [(now, 2)], 'tpl_tunnel_2': [(now, 1)]})
    assert [(a[4], a[6]) for a in applied] == [('series:tpl_tunnel_1', True), ('series:tpl_tunnel_2', False)]


@pytest.mark.asyncio
async def test_worker_preserves_incident_during_missing_data_or_parent_outage(monkeypatch):
    assert not (await run(monkeypatch, rule(), {}))[0]
    assert not (await run(monkeypatch, rule(), {}, {'parent_device_id': 'p'}))[0]


@pytest.mark.asyncio
async def test_notification_success_requires_dispatch_count(monkeypatch):
    from app.services import alert_schedule
    monkeypatch.setattr(alert_schedule, 'get_configured_timezone', AsyncMock(return_value='UTC'))
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *a: True)
    sink = AsyncMock(return_value=0)
    monkeypatch.setattr(worker, 'dispatch_to_channels', sink)
    assert await worker._notify(None, rule(), 'fixture') is False
    sink.return_value = 1
    assert await worker._notify(None, rule(), 'fixture') is True
    assert sink.call_args.args[2]['hostname'] == 'fixture'


@pytest.mark.asyncio
async def test_network_cooldown_suppresses_repeat_trigger_but_not_recovery(monkeypatch):
    from app.services import alert_schedule
    db = NS(execute=AsyncMock(return_value=NS(first=lambda: (1,))))
    sink = AsyncMock(return_value=1)
    monkeypatch.setattr(worker, 'dispatch_to_channels', sink)
    monkeypatch.setattr(alert_schedule, 'get_configured_timezone', AsyncMock(return_value='UTC'))
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *a: True)
    r = rule(cooldown=300)
    assert await worker._notify(db, r, 'fixture', device_id='d', if_index=7) is False
    assert sink.await_count == 0
    assert await worker._notify(db, r, 'fixture', device_id='d', if_index=7, is_recovery=True) is True


def test_interface_baseline_zero_is_unknown_but_stationary_counter_zero_is_valid(monkeypatch):
    from app.core import database
    rows = [('d', 1, 100, 0, 0, 0, 0, 0, 0, 1, 100, 100),
            ('d', 1, 160, 0, 0, 0, 0, 0, 0, 1, 10, 10),
            ('d', 1, 220, 0, 0, 0, 0, 0, 0, 1, 10, 10)]
    monkeypatch.setattr(database, 'get_clickhouse_client', lambda: NS(query=lambda *a, **k: NS(result_rows=rows)))
    _, interfaces = network_history.fetch_history(['if_in_bps'], 0, 300)
    assert interfaces[('d', 1)]['if_in_bps'] == [(100, None), (160, None), (220, 0)]
