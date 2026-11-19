from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
import hashlib
import pytest
from app.api.v1.netpath import _probe_dict
from app.api.v1 import snmp


@pytest.mark.parametrize('enabled,age,expected', [(False, 10, 'disabled'), (True, None, 'pending'),
                                               (True, 179, 'ok'), (True, 180, 'stale')])
def test_netpath_current_state_does_not_reuse_stale_demo_health(enabled, age, expected):
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    out = _probe_dict(dict(id=uuid4(), enabled=enabled, interval_s=60, last_status='ok',
                          last_run_at=None if age is None else now-timedelta(seconds=age)), now=now)
    assert out['last_status'] == expected
    assert out['last_observed_status'] == 'ok'


@pytest.mark.asyncio
async def test_mib_api_compiles_in_child_and_resolves_then_detects_stale_index(tmp_path, monkeypatch):
    from fastapi import HTTPException
    data = b'''FIXTURE-MIB DEFINITIONS ::= BEGIN
    IMPORTS enterprises FROM SNMPv2-SMI;
    fixtureRoot OBJECT IDENTIFIER ::= { enterprises 55557 }
    END'''
    (tmp_path / 'fixture.mib').write_bytes(data)
    rows = [dict(filename='fixture.mib', sha256=hashlib.sha256(data).hexdigest())]
    db = NS(execute=AsyncMock(side_effect=lambda *a: NS(mappings=lambda: NS(all=lambda: rows))))
    monkeypatch.setattr(snmp, 'MIB_DIR', tmp_path)
    assert (await snmp.compile_mibs(db, None))['compiled_modules'] == ['FIXTURE-MIB']
    assert (await snmp.mib_objects(symbol='FIXTURE-MIB::fixtureRoot', db=db, user=None))['oid'].endswith('.55557')
    rows[0]['sha256'] = 'changed'
    with pytest.raises(HTTPException) as exc:
        await snmp.mib_objects(db=db, user=None)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_template_export_import_preserves_profile_and_omits_device_identity(monkeypatch):
    # Templates moved to app/api/v1/device_templates.py (Device Templates, migrate-189): export is
    # format 2 (content plus source identity) and import still accepts format 1 bundles.
    from app.api.v1 import device_templates as dt
    profile = dict(id=uuid4(), name='Fixture', vendor='Test', description='Offline fixture', match_rules={},
                   oid_groups=[], device_type=None, icon=None, match_priority=100, builtin_key=None,
                   revision=1, parent_id=None)
    monkeypatch.setattr(dt.svc, 'get', AsyncMock(return_value=profile))
    bundle = await dt.export_profile(uuid4(), None, None)
    assert bundle['format_version'] == 2
    assert {k: bundle['profile'][k] for k in ('name', 'vendor', 'description', 'match_rules', 'oid_groups')} == \
        {k: profile[k] for k in ('name', 'vendor', 'description', 'match_rules', 'oid_groups')}
    assert 'device_id' not in bundle['profile'] and 'credentials' not in bundle['profile']
    create = AsyncMock(return_value=profile)
    monkeypatch.setattr(dt, '_create', create)
    monkeypatch.setattr(dt.svc, 'name_taken', AsyncMock(return_value=False))
    v1 = {'format_version': 1, 'profile': {k: profile[k] for k in ('name', 'vendor', 'description', 'match_rules', 'oid_groups')}}
    assert await dt.import_profile(dt.ProfileBundle(**v1), None, None) == profile
    assert create.call_args.args[1]['name'] == 'Fixture'
