"""A PostgreSQL candidate cannot bypass infrastructure fencing on stale input."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[2]/'appliance/ha'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('pre_promote',ROOT/'pre_promote.py')
hook=importlib.util.module_from_spec(spec);spec.loader.exec_module(hook)

@pytest.fixture
def gate(tmp_path,monkeypatch):
    program=tmp_path/'fence';program.write_text('#!/bin/false\n');program.chmod(0o755)
    cfg={'cell_id':'lab','node_id':'b','system_identifier':'12345678901','database':'lab','fence_program':str(program)}
    path=tmp_path/'promotion.json';path.write_text(json.dumps(cfg));path.chmod(0o644)
    original=Path.stat
    def stat(p,*a,**kw):
        s=original(p,*a,**kw)
        return SimpleNamespace(st_mode=s.st_mode,st_uid=0)
    monkeypatch.setattr(Path,'stat',stat)
    state=SimpleNamespace(owner='a',system_id=cfg['system_identifier'],states='inactive\ninactive',receipt='off',requests=[])
    def run(args,**kw):
        if args[:2]==['systemctl','list-unit-files']:
            out='zenplus-api.service disabled\nzenplus-poller.service disabled'
        elif args[:2]==['systemctl','show']:
            out=state.states if 'ActiveState' in args else 'MainPID=0\nControlGroup=\n'
        elif args[0]=='/usr/bin/psql':out=state.system_id+'\n'+state.owner+'|00000000-0000-0000-0000-000000000001\n'
        else:
            r=json.loads(kw['input']);state.requests.append(r)
            out=json.dumps({**r,'power_state':state.receipt})
        return SimpleNamespace(stdout=out)
    monkeypatch.setattr(hook.subprocess,'run',run)
    return path,state

def test_fences_predecessor_with_a_bound_receipt(gate):
    path,state=gate;hook.run(path)
    assert len(state.requests)==1 and state.requests[0]['node_id']=='a'

@pytest.mark.parametrize('states',['active\ninactive','inactive',''])
def test_running_consumer_or_incomplete_inventory_blocks_promotion(gate,states):
    path,state=gate;state.states=states
    with pytest.raises(RuntimeError):hook.run(path)
    assert not state.requests

def test_wrong_database_incarnation_blocks_promotion(gate):
    path,state=gate;state.system_id='99999999999'
    with pytest.raises(ValueError):hook.run(path)
    assert not state.requests

def test_no_positive_poweroff_receipt_blocks_promotion(gate):
    path,state=gate;state.receipt='unknown'
    with pytest.raises(ValueError):hook.run(path)

def test_same_controller_does_not_power_off_itself(gate):
    path,state=gate;state.owner='b';hook.run(path)
    assert not state.requests
