import importlib.util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[2]/'appliance/ha'
sys.path.insert(0, str(ROOT))
import analytics_profile as profile
import analytics_schema as schema


def inventory():
    return {'cluster':'test-cell','profile':'strict','nodes':[
        {'name':f'node-{i}','address':f'192.0.2.{i}','data':True} for i in range(1,4)]}


def credentials():
    return {k: c*32 for k,c in [('application','a'),('administration','b'),('replication','c'),('keeper','d')]}


def test_transport_and_witness_boundaries():
    files = profile.render(inventory(),'node-1',credentials())
    ch, keeper = (ET.fromstring(files[n]) for n in ('clickhouse.xml','keeper.xml'))
    assert ch.findtext('listen_host') == '127.0.0.1'
    assert ch.findtext('interserver_listen_host') == '192.0.2.1'
    assert ch.findtext('interserver_https_host') == '192.0.2.1'
    assert ch.find('interserver_http_port') is None
    assert keeper.find('keeper_server/tcp_port') is None
    assert keeper.findtext('path') == profile.ROOT+'/keeper/'
    assert keeper.findtext('keeper_server/storage_path') == profile.ROOT+'/keeper'
    assert keeper.findtext('keeper_server/raft_configuration/secure') == 'true'
    assert keeper.findtext('keeper_server/coordination_settings/quorum_reads') == 'true'
    assert len(keeper.findall('keeper_server/raft_configuration/server')) == 3
    for root in (ch, keeper):
        for kind in ('server','client'):
            assert root.findtext(f'openSSL/{kind}/verificationMode') == 'strict'
            assert root.findtext(f'openSSL/{kind}/loadDefaultCAFile') == 'false'
    assert all(n.findtext('secure') == '1' for n in ch.findall('zookeeper/node'))
    assert ch.findtext('profiles/ha_application/constraints/insert_quorum/min') == '2'
    assert ch.find('profiles/default') is not None
    assert ch.findtext('merge_tree/fsync_after_insert') == '1'
    assert ch.findtext('merge_tree/fsync_part_directory') == '1'
    inv = inventory(); inv['profile']='economy'; inv['nodes'][2]['data']=False
    witness = profile.render(inv,'node-3',credentials())
    assert set(witness) == {'keeper.xml'}
    assert all(v not in witness['keeper.xml'] for v in credentials().values())


def test_secret_and_inventory_rejection():
    creds = credentials(); creds['keeper'] = '<xml>invalid</xml>'
    with pytest.raises(ValueError): profile.render(inventory(),'node-1',creds)
    with pytest.raises(ValueError): profile.render(inventory(),'absent',credentials())
    inv=inventory(); inv['nodes'][1]['address']='192.0.2.1'
    with pytest.raises(ValueError): profile.render(inv,'node-1',credentials())


def test_private_render_refuses_to_overwrite_an_existing_cell(tmp_path):
    target=tmp_path/'cell'
    files=profile.render(inventory(),'node-1',credentials())
    profile.write_new(target,files)
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    with pytest.raises(FileExistsError):profile.write_new(target,files)
    assert (target/'client.xml').read_text()==files['client.xml']


@pytest.mark.parametrize('engine,args',[('MergeTree',''),('MergeTree','()'),
    ('ReplacingMergeTree','(version)'),('AggregatingMergeTree','()'),('SummingMergeTree','(value)')])
def test_schema_preserves_engine_parameters_and_column_expressions(engine,args):
    ddl = "CREATE TABLE zenplus.samples (value UInt64, version UInt64, note String DEFAULT 'ENGINE = MergeTree()') ENGINE = "+engine+args+' ORDER BY value'
    result = schema.replicated_ddl({'name':'samples','engine':engine,'create_table_query':ddl},'test-cell')
    assert "note String DEFAULT 'ENGINE = MergeTree()'" in result
    assert 'ENGINE = Replicated'+engine+args+' ORDER BY value' in result
    assert '{uuid}' not in result and '{replica}' not in result
    assert result.endswith(' ORDER BY value')


@pytest.mark.parametrize('ddl',[
    'CREATE TABLE zenplus.samples (value UInt64) ENGINE = Memory',
    'CREATE TABLE other.samples (value UInt64) ENGINE = MergeTree ORDER BY value',
    'CREATE TABLE zenplus.samples (value UInt64) ENGINE = MergeTree ORDER BY value; DROP DATABASE zenplus',
])
def test_reject_unexpected_schema(ddl):
    with pytest.raises(ValueError): schema.replicated_ddl({'name':'samples','engine':'MergeTree','create_table_query':ddl},'test-cell')


def test_views_are_installed_after_history_and_internal_targets_rejected():
    row = {'name':'rollup','engine':'MaterializedView','create_table_query':
        'CREATE MATERIALIZED VIEW zenplus.rollup TO zenplus.samples AS SELECT * FROM zenplus.raw'}
    assert schema.plan([row],'test-cell')['views_after_history_import'][0]['sql'] == row['create_table_query']
    row['create_table_query']='CREATE MATERIALIZED VIEW zenplus.rollup ENGINE = MergeTree ORDER BY value AS SELECT * FROM zenplus.raw'
    with pytest.raises(ValueError): schema.plan([row],'test-cell')
    with pytest.raises(ValueError): schema.plan([], 'test-cell')
