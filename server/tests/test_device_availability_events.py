import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.api.v1 import devices
from app.services import report_data_service as rds, report_service

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=24)
DID = uuid4()

@pytest.fixture
def setup(monkeypatch):
    async def get_device(*args): return SimpleNamespace(tags=[])
    async def scope(*args): return None
    async def maintenance(*args, **kwargs): return {}
    monkeypatch.setattr(devices.device_service, 'get_device', get_device)
    monkeypatch.setattr(devices.scoping, 'visible_tags', scope)
    monkeypatch.setattr(devices.scoping, 'entity_visible', lambda *args: True)
    monkeypatch.setattr(report_service, '_fetch_device_maintenance_windows', maintenance)
    monkeypatch.setattr(rds, '_device_transitions', lambda *args: {})

def call(start=START, end=END):
    return asyncio.run(devices.get_device_availability_events(DID, start, end, object(), object()))

def test_isolated_failed_checks_are_not_invented_as_outages(setup):
    result=call()
    assert result['outages'] == 0 and result['downtime_seconds'] == 0
    assert result['basis'] == 'confirmed'


def test_carry_in_historical_recovery_and_selected_bounds(setup, monkeypatch):
    def transitions(start,end,ids):
        assert (start,end,ids)==(START,END,[str(DID)])
        return {str(DID):[(START-timedelta(hours=1),START+timedelta(minutes=10)),(END-timedelta(minutes=5),END+timedelta(hours=1))]}
    monkeypatch.setattr(rds,'_device_transitions',transitions)
    result=call()
    assert result['outages']==2
    assert result['downtime_seconds']==900
    assert all(not event['ongoing'] for event in result['events'])


def test_maintenance_uses_same_contract_as_report(setup,monkeypatch):
    async def maintenance(*args, **kwargs):return {str(DID):[(START.replace(tzinfo=None),(START+timedelta(hours=1)).replace(tzinfo=None))]}
    monkeypatch.setattr(report_service,'_fetch_device_maintenance_windows',maintenance)
    monkeypatch.setattr(rds,'_device_transitions',lambda *args:{str(DID):[(START,START+timedelta(minutes=10))]})
    assert call()['outages']==0


def test_failure_is_unknown_not_zero_outages(setup,monkeypatch):
    def failed(*args):raise RuntimeError('private connection details')
    monkeypatch.setattr(rds,'_device_transitions',failed)
    with pytest.raises(HTTPException) as exc:call()
    assert exc.value.status_code==503
    assert 'private' not in exc.value.detail


def test_hidden_device_never_queries_history(setup,monkeypatch):
    monkeypatch.setattr(devices.scoping,'entity_visible',lambda *args:False)
    monkeypatch.setattr(rds,'_device_transitions',lambda *args:pytest.fail('scoped history queried'))
    with pytest.raises(HTTPException) as exc:call()
    assert exc.value.status_code==404


@pytest.mark.parametrize('end',[START,START-timedelta(seconds=1),START+timedelta(days=367)])
def test_invalid_range_is_rejected(setup,end):
    with pytest.raises(HTTPException) as exc:call(end=end)
    assert exc.value.status_code==422


def test_output_caps_event_list_without_truncating_totals(setup,monkeypatch):
    eps=[(START+timedelta(minutes=2*i),START+timedelta(minutes=2*i+1)) for i in range(101)]
    monkeypatch.setattr(rds,'_device_transitions',lambda *args:{str(DID):eps})
    result=call()
    assert result['outages']==101 and result['downtime_seconds']==6060
    assert len(result['events'])==100 and result['events_truncated']


def test_maintenance_failure_is_not_silently_ignored(setup, monkeypatch):
    async def failed(*args, **kwargs):
        assert kwargs == {"device_ids": [str(DID)], "strict": True}
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(report_service, "_fetch_device_maintenance_windows", failed)
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 503


def test_strict_maintenance_reader_scopes_query():
    class DB:
        async def execute(self, statement, params):
            assert "d.id = ANY(CAST(:device_ids AS uuid[]))" in str(statement)
            assert params["device_ids"] == [str(DID)]
            raise RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        asyncio.run(report_service._fetch_device_maintenance_windows(DB(), START, END, device_ids=[str(DID)], strict=True))
