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
    profile = dict(name='Fixture', vendor='Test', description='Offline fixture', match_rules={}, oid_groups=[])
    db = NS(execute=AsyncMock(return_value=NS(mappings=lambda: NS(first=lambda: profile))))
    bundle = await snmp.export_profile(uuid4(), db, None)
    expected = snmp.ProfileCreate(**profile).model_dump(mode='json')
    assert bundle['profile'] == expected
    assert 'device_id' not in bundle and 'credentials' not in bundle
    create = AsyncMock(return_value=profile)
    monkeypatch.setattr(snmp, 'create_profile', create)
    assert await snmp.import_profile(snmp.ProfileBundle(**bundle), db, None) == profile
    assert create.call_args.args[0].model_dump(mode='json') == expected
