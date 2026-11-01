"""Read-only list API contracts on a disposable PostgreSQL cluster."""
from uuid import uuid4
import pytest
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from app.services.list_ordering import natural_sql


def seed(db, name, ip, *, server_name=None, version="1.14.4"):
    sid, aid = uuid4(), uuid4()
    db.execute("INSERT INTO servers(id,display_name,hostname,primary_ip) VALUES ($1,$2,$3,$4::inet)", sid, server_name or name, name, ip)
    db.execute("""INSERT INTO agents(id,server_id,agent_uid,hostname,version,last_ip,status,last_heartbeat_at)
                  VALUES($1,$2,$3,$4,$5,$6::inet,'online',now())""", aid, sid, str(aid), name, version, ip)
    return str(aid), str(sid)


def get(client, path, **params):
    response = client.get('/api/v1/' + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_natural_sort_handles_numbers_without_overflow(pg_db):
    values = ['host10', 'HOST2', 'host1', 'host' + '9' * 90, 'host' + '1' * 91]
    key = natural_sql('v')
    rows = pg_db.query(f'SELECT v FROM unnest($1::text[]) v ORDER BY {key}', values)
    assert [r[0] for r in rows] == [values[i] for i in (2, 1, 0, 3, 4)]


def test_fleet_search_covers_server_names_ips_and_literal_text(pg_db):
    seed(pg_db, 'host10', '192.168.8.10', server_name='Payment node')
    seed(pg_db, 'host2', '192.168.8.2')
    seed(pg_db, 'x-host1', '10.0.0.1')
    seed(pg_db, 'pct%_host', '10.0.0.2')
    with api_client(pg_db, role='admin') as client:
        result = get(client, 'agent-fleet', q=' 192.168.8. ')
        assert result['total'] == 2
        assert [r['last_ip'] for r in result['items']] == ['192.168.8.2', '192.168.8.10']
        assert get(client, 'agent-fleet', q='Payment')['items'][0]['hostname'] == 'host10'
        assert [r['hostname'] for r in get(client, 'agent-fleet', q='HOST')['items']] == ['host2', 'host10', 'pct%_host', 'x-host1']
        assert get(client, 'agent-fleet', q='%_')['total'] == 1
        # Explicit sorting overrides automatic match ordering.
        assert [r['last_ip'] for r in get(client, 'agent-fleet', q='192.168.8.', sort='last_ip', order='desc')['items']] == ['192.168.8.10', '192.168.8.2']


@pytest.mark.parametrize('column', ['hostname','platform','version','status','authorization','policy','update_ring','last_heartbeat_at','monitoring','queue_depth','spool_bytes','last_ip'])
def test_every_fleet_data_sort_is_valid_and_repeatable(pg_db, column):
    for i in (10, 2, 1):
        seed(pg_db, 'host' + str(i), f'192.168.8.{i}', version='1.14.' + str(i))
    with api_client(pg_db, role='admin') as client:
        for direction in ('asc', 'desc'):
            first = get(client, 'agent-fleet', sort=column, order=direction)
            second = get(client, 'agent-fleet', sort=column, order=direction)
            assert [r['id'] for r in first['items']] == [r['id'] for r in second['items']]
        versions = get(client, 'agent-fleet', sort='version')['items']
        assert [r['version'] for r in versions] == ['1.14.1', '1.14.2', '1.14.10']


def test_fleet_paging_and_default_order_survive_heartbeats(pg_db):
    pg_db.execute("""INSERT INTO agents(agent_uid,hostname,last_ip,status,last_heartbeat_at)
        SELECT 'sort-test-'||n, 'host'||n, ('192.168.8.'||n)::inet, 'online', now() - n*interval '1 second'
        FROM generate_series(1,205) n""")
    with api_client(pg_db, role='admin') as client:
        def pages(**extra):
            return [r for p in range(1,6) for r in get(client,'agent-fleet',page=p,page_size=50,**extra)['items']]
        initial = pages()
        assert len({r['id'] for r in initial}) == 205
        assert [r['hostname'] for r in initial] == ['host'+str(i) for i in range(1,206)]
        pg_db.execute("UPDATE agents SET last_heartbeat_at=now()+(random()*1000)*interval '1 second'")
        assert [r['id'] for r in pages()] == [r['id'] for r in initial]
        assert [r['last_ip'] for r in pages(q='192.168.8.')] == ['192.168.8.'+str(i) for i in range(1,206)]


def test_servers_and_devices_share_prefix_and_numeric_order(pg_db):
    for i in (10, 2, 1):
        seed(pg_db, 'host'+str(i), '192.168.8.'+str(i))
        pg_db.execute("INSERT INTO devices(hostname,ip_address) VALUES($1,$2::inet)", 'host'+str(i), '192.168.8.'+str(i))
    seed(pg_db, 'a-host0', '203.0.113.201')
    pg_db.execute("INSERT INTO devices(hostname,ip_address) VALUES('a-host0','203.0.113.201')")
    with api_client(pg_db, role='admin') as client:
        for path, result_key, ip_key, query_key in [('servers','items','primary_ip','q'),('devices','data','ip_address','search')]:
            results = get(client,path,**{query_key:'192.168.8.'})[result_key]
            assert [r[ip_key] for r in results] == ['192.168.8.'+str(i) for i in (1,2,10)]
            results = get(client,path,**{query_key:'host'})[result_key]
            assert [r['hostname'] for r in results] == ['host1','host2','host10','a-host0']


def test_search_does_not_expand_server_visibility(pg_db):
    seed(pg_db, 'private-host', '192.168.8.2')
    with api_client(pg_db, role='search-team', permissions=['servers.view'], role_scope_tags=('team-a',)) as client:
        assert get(client, 'servers', q='192.168.8.')['total'] == 0


def test_asset_best_match_and_explicit_name_sort_are_distinct(pg_db):
    for name, ip in [('host10','192.168.8.10'),('host2','192.168.8.2'),('a-host1','203.0.113.202')]:
        seed(pg_db, name, ip)
    with api_client(pg_db, role='admin') as client:
        rows = get(client,'inventory/assets',q='host',sort='match')['items']
        assert [r['name'] for r in rows] == ['host2','host10','a-host1']
        rows = get(client,'inventory/assets',q='host',sort='name')['items']
        assert [r['name'] for r in rows] == ['a-host1','host2','host10']
        rows = get(client,'inventory/assets',q='192.168.8.',sort='match')['items']
        assert [r['ip'] for r in rows] == ['192.168.8.2','192.168.8.10']


def test_service_search_prefers_name_prefix_within_status_priority(pg_db):
    for name in ('host10','host2','a-host1'):
        pg_db.execute("INSERT INTO service_checks(name,check_type,target_host,target_port,status) VALUES($1,'tcp','192.0.2.1',443,'down')",name)
    with api_client(pg_db, role='admin') as client:
        for sort in ('name','status'):
            rows = get(client,'service-checks',search='host',sort=sort)['data']
            assert [r['name'] for r in rows] == ['host2','host10','a-host1']


def test_device_sorts_are_applied_before_the_result_limit(pg_db):
    pg_db.execute("""INSERT INTO devices(hostname,ip_address,os_version)
        SELECT 'order-host'||(206-n), ('198.51.100.'||n)::inet, '1.'||n
        FROM generate_series(1,205) n""")
    with api_client(pg_db, role='admin') as client:
        for column in ('hostname','ip_address','device_type','vendor','model','os_version','location','group_name','status','last_rtt_ms','last_seen'):
            result = get(client,'devices',search='order-host',sort=column,order='desc',limit=5)
            assert result['meta']['total'] == 205 and len(result['data']) == 5
        rows = get(client,'devices',search='order-host',sort='os_version',order='desc',limit=5)['data']
        assert [r['os_version'] for r in rows] == ['1.'+str(n) for n in range(205,200,-1)]
        rows = get(client,'devices',search='198.51.100.',limit=5)['data']
        assert [r['ip_address'] for r in rows] == ['198.51.100.'+str(n) for n in range(1,6)]
