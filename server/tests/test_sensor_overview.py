from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.services import sensor_overview as overview


def test_connection_does_not_trust_stale_online_status():
    now=datetime.now(timezone.utc)
    sensor={'status':'online','api_key_hash':'not-exposed','last_heartbeat_at':now-timedelta(seconds=200)}
    assert overview.sensor_connection(sensor,now)=='offline'
    sensor['last_heartbeat_at']=now-timedelta(seconds=100)
    assert overview.sensor_connection(sensor,now)=='degraded'
    sensor['last_heartbeat_at']=now
    assert overview.sensor_connection(sensor,now)=='online'
    sensor['bootstrap_config']={'authorization_pending':True}
    assert overview.sensor_connection(sensor,now)=='pending'
    sensor['status']='disabled'
    assert overview.sensor_connection(sensor,now)=='disabled'


def test_observations_are_sensor_specific_and_keep_missing_results_unknown():
    now=datetime.now(timezone.utc);did,sid=uuid4(),uuid4()
    device={'id':did,'name':'router','address':'192.0.2.1','assignment_source':'Device group','ping_enabled':True,'snmp_enabled':True}
    service={'id':sid,'name':'health','check_type':'http','enabled':True,'assignment_source':'Direct','needs_auth_support':False}
    measured={'ping':{str(did):{'last_result_at':now,'is_up':0,'latency_ms':0}},'service':{str(sid):{'last_result_at':now,'is_up':1}}}
    devices,services=overview.target_results([device],[service],measured,'online','1.23.5',now)
    assert devices[0]['checks']['ping']['state']=='down'
    assert devices[0]['checks']['snmp']['state']=='no_data'
    assert services[0]['checks']['service']['state']=='up'
    devices,services=overview.target_results([device],[service],measured,'offline','1.23.5',now)
    assert services[0]['checks']['service']['state']=='probe_offline'
    assert devices[0]['checks']['ping']['state']=='probe_offline'


def test_old_sensor_displays_required_update_for_authenticated_checks():
    row={'id':uuid4(),'name':'auth','check_type':'http','enabled':True,'needs_auth_support':True,'assignment_source':'Direct'}
    _,items=overview.target_results([],[row],{},'online','1.23.4',datetime.now(timezone.utc))
    assert items[0]['checks']['service']['state']=='update_required'
    row['enabled']=False
    _,items=overview.target_results([],[row],{},'online','1.23.4',datetime.now(timezone.utc))
    assert items[0]['checks']['service']['state']=='disabled'

@pytest.mark.asyncio
async def test_inventory_respects_target_visibility(monkeypatch):
    monkeypatch.setattr(overview.scoping,'visible_tags',AsyncMock(return_value=['branch-a']))
    rows=[{'id':uuid4(),'tags':['branch-a']},{'id':uuid4(),'tags':['branch-b']}]
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:rows))))
    devices,services=await overview.assigned_targets(db,uuid4(),None)
    assert devices==[rows[0]] and services==[rows[0]]


def test_metrics_queries_are_bound_to_this_sensor_and_assigned_ids(monkeypatch):
    sensor=uuid4();device=uuid4();calls=[]
    def query(sql,parameters):
        calls.append(parameters)
        assert 'poller_id={sensor:String}' in sql
        assert parameters=={'sensor':str(sensor),'ids':[str(device)]}
        return SimpleNamespace(named_results=lambda:[])
    monkeypatch.setattr(overview,'get_clickhouse_client',lambda:SimpleNamespace(query=query))
    assert overview.sensor_measurements(sensor,[{'id':device}],[])=={'ping':{},'snmp':{},'service':{}}
    assert len(calls)==2

@pytest.mark.asyncio
async def test_missing_sensor_does_not_query_measurements():
    from app.api.v1.sensors import sensor_overview
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:None))))
    with pytest.raises(HTTPException) as e:await sensor_overview(uuid4(),None,db,None)
    assert e.value.status_code==404


def test_viewer_without_settings_access_is_rejected(client,as_viewer):
    assert client.get('/api/v1/sensors/'+str(uuid4())+'/overview').status_code==403


@pytest.mark.asyncio
async def test_history_outage_preserves_assignments_without_false_success(monkeypatch):
    from app.api.v1 import sensors, sensor_api
    now=datetime.now(timezone.utc)
    row={'id':uuid4(),'name':'test-sensor','status':'online','api_key_hash':'secret-never-returned','last_heartbeat_at':now,'created_at':now,'updated_at':now}
    device={'id':uuid4(),'name':'router','assignment_source':'Direct','ping_enabled':True,'snmp_enabled':True}
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:row))))
    monkeypatch.setattr(overview,'assigned_targets',AsyncMock(return_value=([device],[])))
    def unavailable(*args):raise RuntimeError('database-internal-secret')
    monkeypatch.setattr(overview,'sensor_measurements',unavailable)
    monkeypatch.setattr(sensor_api,'_signed_binary_metadata',lambda platform:({},None,None))
    monkeypatch.setattr(sensors,'_server_url',lambda request:'https://controller.example.invalid')
    result=await sensors.sensor_overview(row['id'],None,db,None)
    assert result['measurements_available'] is False
    assert len(result['devices'])==1 and result['devices'][0]['checks']['ping']['state']=='no_data'
    assert result['release']=={'version':None,'available':False}
    assert 'secret-never-returned' not in str(result) and 'database-internal-secret' not in str(result)
