from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
import json
import pytest
from app.api.v1 import alert_engine as engine


class DB:
    def __init__(self, rule): self.rule = rule; self.events = set(); self.alerts = []; self.stamps = []
    async def execute(self, sql, params=None):
        sql = str(sql); first = None; rows = []
        if 'FROM alert_rules' in sql: rows = [self.rule]
        elif 'trap_event_id' in sql and 'SELECT 1' in sql:
            first = (1,) if params['event_id'] in self.events else None
        elif 'INSERT INTO alerts' in sql:
            meta = json.loads(params['metadata']); self.events.add(meta['trap_event_id'])
            self.alerts.append(params); first = (uuid4(),)
        elif 'UPDATE alerts' in sql: self.stamps.append(json.loads(params['m']))
        return NS(first=lambda: first, fetchall=lambda: rows)
    async def commit(self): pass


@pytest.mark.asyncio
async def test_trap_event_replay_dispatches_once_to_local_sink(monkeypatch):
    from app.services import host_alert_service
    r = NS(id=uuid4(), name='Link down', metric='trap', operator='==', threshold=0,
           device_id=None, group_id=None, scope_tag=None, trap_oid='1.3.6.1.6.3.1.1.5.3',
           severity='critical', notify_channels=['fixture-sink'], cooldown=60,
           schedule_start=None, schedule_end=None, schedule_days=None,
           email_subject=None, email_body=None, sms_template=None, conditions=None, condition_logic='AND')
    db = DB(r); sink = AsyncMock(return_value=1)
    monkeypatch.setattr(host_alert_service, 'dispatch_to_channels', sink)
    monkeypatch.setattr(engine, 'get_configured_timezone', AsyncMock(return_value='UTC'))
    monkeypatch.setattr(engine, '_dashboard_url', AsyncMock(return_value=''))
    event = engine.TrapEvent(event_id=uuid4(), source_ip='2001:db8::1', trap_oid=r.trap_oid, message='fixture link down')
    assert (await engine.evaluate_trap(event, db))['alerts_created'] == 1
    assert (await engine.evaluate_trap(event, db))['alerts_created'] == 0
    assert sink.await_count == 1 and db.stamps == [{'notified': True}]
    assert json.loads(db.alerts[0]['metadata'])['source_ip'] == '2001:db8::1'


@pytest.mark.asyncio
async def test_trap_parent_failure_suppresses_event_alert(monkeypatch):
    db = NS(execute=AsyncMock(return_value=NS(first=lambda: NS(group_id=None, tags=[], hostname='child', ip='192.0.2.1'))))
    monkeypatch.setattr(engine, '_device_in_maintenance', AsyncMock(return_value=False))
    monkeypatch.setattr(engine, '_find_suppressing_dependency', AsyncMock(return_value={'parent_device_id': 'parent'}))
    out = await engine.evaluate_trap(engine.TrapEvent(device_id=str(uuid4()), source_ip='192.0.2.1', trap_oid='1.2.3'), db)
    assert out == {'alerts_created': 0, 'suppressed': 'dependency'}
