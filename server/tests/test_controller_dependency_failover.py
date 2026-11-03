"""Persistent app failure relinquishes DB leadership without bypassing fencing."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock
import pytest

ROOT=Path(__file__).resolve().parents[2]/'appliance/ha'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('controller_dependency',ROOT/'controller.py')
controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)


@pytest.fixture
def supervisor(tmp_path,monkeypatch):
    value=controller.Supervisor.__new__(controller.Supervisor)
    value.config={'cell_id':'lab','node_id':'node-a','dependency_failure_seconds':30}
    value.boot='test-boot';value.dependency_failure_since=None
    value.local_primary=AsyncMock();value.stop=AsyncMock()
    monkeypatch.setattr(controller,'WITHDRAWN',tmp_path/'withdrawn.json')
    clock=[100.0]
    monkeypatch.setattr(controller.time,'monotonic',lambda:clock[0])
    calls=[]
    async def command(*args,**kwargs):
        assert value.stop.await_count==1
        assert json.loads(controller.WITHDRAWN.read_text())['node_id']=='node-a'
        calls.append(args)
        return 0,''
    monkeypatch.setattr(controller,'command',command)
    return value,clock,calls


@pytest.mark.asyncio
async def test_transient_failure_does_not_demote_or_quarantine(supervisor):
    value,clock,calls=supervisor
    assert not await value.dependency_failure()
    clock[0]+=29
    assert not await value.dependency_failure()
    assert not calls and not controller.WITHDRAWN.exists()
    value.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_failure_stops_consumers_and_persists_quarantine_before_withdrawal(supervisor):
    value,clock,calls=supervisor
    await value.dependency_failure();clock[0]+=30
    assert await value.dependency_failure()
    value.local_primary.assert_awaited_once()
    assert calls==[('systemctl','--no-block','stop','zenplus-ha-patroni.service')]
    assert controller.WITHDRAWN.stat().st_mode&0o777==0o600


@pytest.mark.asyncio
async def test_lost_primary_status_cannot_trigger_stale_withdrawal(supervisor):
    value,clock,calls=supervisor
    await value.dependency_failure();clock[0]+=30
    value.local_primary.side_effect=controller.authority.AuthorityUnavailable('standby')
    with pytest.raises(controller.authority.AuthorityUnavailable):await value.dependency_failure()
    assert not calls and not controller.WITHDRAWN.exists()


@pytest.mark.asyncio
async def test_failure_to_stop_consumers_does_not_release_database(supervisor):
    value,clock,calls=supervisor
    await value.dependency_failure();clock[0]+=30
    value.stop.side_effect=RuntimeError('consumer still active')
    with pytest.raises(RuntimeError):await value.dependency_failure()
    assert not calls and not controller.WITHDRAWN.exists()


@pytest.mark.asyncio
async def test_quarantine_blocks_authority_before_network_or_database_access(supervisor):
    value,clock,calls=supervisor
    controller.WITHDRAWN.write_text('{}')
    with pytest.raises(controller.authority.AuthorityUnavailable,match='quarantined'):
        await value.eligible()
    value.local_primary.assert_not_awaited()
