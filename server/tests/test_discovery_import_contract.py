from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import discovery_v2 as api
from app.schemas.discovery_v2 import ImportRequest
from app.models.discovery_v2 import DiscoveryProfile, DiscoveryRun, DiscoveryResultV2
from app.models.device import Device


class DB:
    def __init__(self, existing=None):
        self.existing = existing; self.added = []; self.sql = []
        self.run = NS(id=uuid4(), profile_id=uuid4())
        self.profile = NS(id=self.run.profile_id, default_location='Profile-Site')
        self.result = NS(run_id=self.run.id, imported=False, ip_address='192.0.2.1',
            hostname='fixture', sys_name='fixture', device_type='switch', sys_object_id='1.3.6.1.4.1.9',
            vendor='Cisco', model='test', os_version='fixture', suggested_group_id=None,
            suggested_tags=[], matched_template_id=None, credential_used=uuid4())
    async def get(self, kind, key):
        if kind == DiscoveryRun: return self.run
        if kind == DiscoveryProfile: return self.profile
        return self.result
    def add(self, item):
        if getattr(item, 'id', None) is None: item.id = uuid4()
        self.added.append(item)
    async def flush(self): pass
    async def commit(self): pass
    async def execute(self, sql, params=None):
        self.sql.append(str(sql))
        return NS(first=lambda: (self.existing,) if self.existing and 'SELECT id FROM devices' in str(sql) else None)
    @asynccontextmanager
    async def begin_nested(self): yield self


@pytest.fixture(autouse=True)
def applied(monkeypatch):
    calls = []
    async def fake_apply(db, device, credential_id):
        calls.append((device, credential_id))
        device.snmp_community = 'encrypted-from-credential'
    monkeypatch.setattr(api, 'apply_credential', fake_apply)
    return calls


@pytest.mark.asyncio
async def test_rediscovery_updates_identity_without_duplicate_or_overwriting_monitoring(monkeypatch):
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    db = DB(uuid4())
    out = await api.import_results(db.run.id, ImportRequest(result_ids=[1], conflict_strategy='update'), db, NS(id=uuid4()))
    assert out.successful == 1 and out.devices_created == 0
    assert not any(isinstance(item, Device) for item in db.added)
    statement = next(s for s in db.sql if s.startswith('UPDATE devices'))
    assert 'ping_enabled' not in statement and 'snmp_credential_id' not in statement


@pytest.mark.asyncio
async def test_new_import_uses_valid_discovery_credential_and_retry_skips(monkeypatch):
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    db = DB(); user = NS(id=uuid4()); payload = ImportRequest(result_ids=[1])
    out = await api.import_results(db.run.id, payload, db, user)
    node = next(item for item in db.added if isinstance(item, Device))
    assert node.snmp_enabled and node.snmp_credential_id == db.result.credential_used
    assert out.devices_created == 1
    assert (await api.import_results(db.run.id, payload, db, user)).skipped == 1


@pytest.mark.asyncio
async def test_import_copies_credential_onto_device_for_the_poller(monkeypatch, applied):
    # ZP-01: the poller reads SNMP auth from the device row; a bare
    # credential link polled with a blank community forever.
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    db = DB()
    await api.import_results(db.run.id, ImportRequest(result_ids=[1]), db, NS(id=uuid4()))
    node = next(item for item in db.added if isinstance(item, Device))
    assert applied == [(node, db.result.credential_used)]
    assert node.snmp_community == 'encrypted-from-credential'


@pytest.mark.asyncio
async def test_import_falls_back_to_profile_location_and_normalises_oid(monkeypatch):
    # ZP-05 / ZP-04
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    db = DB(); db.result.sys_object_id = 'iso.3.6.1.4.1.9.1.1208'
    await api.import_results(db.run.id, ImportRequest(result_ids=[1]), db, NS(id=uuid4()))
    node = next(item for item in db.added if isinstance(item, Device))
    assert node.location == 'Profile-Site'
    assert node.sys_object_id == '.1.3.6.1.4.1.9.1.1208'


@pytest.mark.asyncio
async def test_import_location_in_request_wins_over_profile(monkeypatch):
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    db = DB()
    await api.import_results(db.run.id, ImportRequest(result_ids=[1], location='Chosen'), db, NS(id=uuid4()))
    node = next(item for item in db.added if isinstance(item, Device))
    assert node.location == 'Chosen'
