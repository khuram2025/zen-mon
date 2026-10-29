import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('controller_units',Path(__file__).resolve().parents[2]/'appliance/ha/controller_units.py')
units=importlib.util.module_from_spec(spec);spec.loader.exec_module(units)


def test_complete_inventory_has_no_ambiguous_owner():
    assert not set(units.MANAGED)&set(units.LOCAL)
    assert set(units.START)<=set(units.MANAGED)
    assert len(units.MANAGED)==len(set(units.MANAGED))
    for name in ['zenplus-api.service','zenplus-poller.service','zenplus-syslog.service','zenplus-netflow-collector.service',
        'zenplus-ncm-worker.service','zenplus-ncm-backup.timer','zenplus-support-queue.path']:
        assert name in units.MANAGED


def test_every_consumer_is_bound_to_supervisor_and_every_service_checks_admission():
    for unit in units.MANAGED:
        text=units.dropin(unit)
        assert 'BindsTo=zenplus-ha-controller.service' in text
        assert 'After=zenplus-ha-controller.service' in text
        if unit.endswith('.service'):
            assert 'KillMode=control-group' in text and 'SendSIGKILL=yes' in text
            assert 'ExecStartPre=/opt/zenplus/venv/bin/python /usr/local/lib/zenplus-ha/admit.py' in text
            assert 'ZENPLUS_CONTROLLER_GRANT=/run/zenplus-controller/grant.json' in text


def test_unknown_unit_or_reused_directory_refused(tmp_path):
    with pytest.raises(ValueError):units.dropin('postgresql.service')
    with pytest.raises(ValueError):units.write_new(tmp_path/'bad',['zenplus-api.service']*2)
    out=tmp_path/'new';units.write_new(out,['zenplus-api.service'])
    with pytest.raises(FileExistsError):units.write_new(out,['zenplus-api.service'])
