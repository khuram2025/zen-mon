import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.api.v1.monitoring_sites import MonitoringSelection, observation, target_row, set_monitoring_sites


def test_selection_defaults_to_controller_and_rejects_no_locations():
    assert MonitoringSelection().controller_enabled is True
    with pytest.raises(ValidationError):
        MonitoringSelection(controller_enabled=False)
    sid = uuid4()
    assert MonitoringSelection(controller_enabled=False, sensor_ids=[sid, sid]).sensor_ids == [sid]


def test_offline_probe_never_looks_like_success_from_old_samples():
    now = datetime.now(timezone.utc)
    recent = {'last_result_at': now, 'is_up': 1, 'availability_pct': 100}
    assert observation(recent, 60, True, 'offline', now)['state'] == 'probe_offline'
    assert observation(recent, 60, False, 'online', now)['state'] == 'disabled'
    assert observation(recent, 60, True, 'pending', now)['state'] == 'probe_pending'


def test_missing_and_expired_samples_are_unknown_and_keep_observed_history():
    now = datetime.now(timezone.utc)
    assert observation(None, 60, True, 'online', now)['state'] == 'no_data'
    old = {'last_result_at': now - timedelta(minutes=5), 'is_up': 1, 'availability_pct': 100}
    result = observation(old, 60, True, 'online', now)
    assert result['state'] == 'no_data'
    assert result['availability_pct'] == 100
    assert observation(dict(old, last_result_at=now, is_up=0), 60, True, 'online', now)['state'] == 'down'


def test_visibility_is_checked_before_exposing_target(monkeypatch):
    async def scope(*args): return ['branch-a']
    monkeypatch.setattr('app.api.v1.monitoring_sites.scoping.visible_tags', scope)
    class Result:
        def mappings(self): return self
        def first(self): return {'tags': ['branch-b']}
    class DB:
        async def execute(self, *args): return Result()
    with pytest.raises(HTTPException) as error:
        asyncio.run(target_row('device', uuid4(), DB(), SimpleNamespace()))
    assert error.value.status_code == 404


def test_remote_auth_requires_updated_probe(monkeypatch):
    sid = uuid4()
    async def target(*args, **kwargs): return {'credential_id': uuid4(), 'workflow_steps': []}
    async def selected(*args): return []
    async def scope(*args): return None
    monkeypatch.setattr('app.api.v1.monitoring_sites.target_row', target)
    monkeypatch.setattr('app.api.v1.monitoring_sites.selected_rows', selected)
    monkeypatch.setattr('app.api.v1.monitoring_sites.scoping.visible_tags', scope)
    class Result:
        def mappings(self): return self
        def first(self): return {'tags': [], 'version': '1.23.4'}
    class DB:
        async def execute(self, statement, params=None):
            assert 'FROM sensors' in str(statement)  # no assignment writes before rejection
            return Result()
    with pytest.raises(HTTPException) as error:
        asyncio.run(set_monitoring_sites('service_check', uuid4(), MonitoringSelection(sensor_ids=[sid]), DB(), None))
    assert error.value.status_code == 400
    assert '1.23.5' in error.value.detail
