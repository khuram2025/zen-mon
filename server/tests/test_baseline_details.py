"""Baseline policy reports on a fully migrated disposable PostgreSQL cluster."""
import json
from uuid import uuid4

import pytest
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401


def policy(db, **over):
    bid=uuid4()
    db.execute("""INSERT INTO software_baselines(id,name,os_type,match_tags,updated_at)
        VALUES($1,$2,'windows',$3::jsonb,now()-interval '1 day')""",bid,'Core '+str(bid),json.dumps(over.get('tags',[])))
    rules=[]
    for name,kind in [('Chrome','required'),('Remote tool','prohibited')]:
        rid=uuid4();rules.append(rid)
        db.execute("""INSERT INTO software_baseline_rules(id,baseline_id,package_match,rule_type,min_version,notes)
            VALUES($1,$2,$3,$4,$5,'Install the approved package')""",rid,bid,name,kind,'150' if kind=='required' else None)
    return bid,rules


def server(db,name='server2',ip='192.168.8.2',*,tags=(),os='windows',status='healthy'):
    sid=uuid4()
    db.execute("INSERT INTO servers(id,display_name,hostname,primary_ip,os_type,status,tags) VALUES($1,$2,$2,$3::inet,$4,$5,$6::jsonb)",sid,name,ip,os,status,json.dumps(tags))
    return sid


def outcome(db,bid,rid,sid,status='compliant',age='1 hour'):
    db.execute("""INSERT INTO server_baseline_results(server_id,rule_id,baseline_id,status,found_package,found_version,expected,severity,evaluated_at,first_failed_at)
        VALUES($1,$2,$3,$4,$5,'149','Chrome >= 150','critical',now()-$6::interval,
               CASE WHEN $4='compliant' THEN NULL ELSE now()-interval '3 days' END)""",sid,rid,bid,status,'Chrome' if status!='missing' else None,age)


def read(client,bid,suffix='coverage',**params):
    r=client.get(f'/api/v1/server-baselines/{bid}/{suffix}',params=params)
    assert r.status_code==200,r.text
    return r.json()


def test_policy_groups_servers_and_includes_unassessed_checks(pg_db):
    bid,rules=policy(pg_db)
    good=server(pg_db,'server10','192.168.8.10')
    bad=server(pg_db,'server2')
    pending=server(pg_db,'server1','192.168.8.1')
    partial=server(pg_db,'partial','192.168.8.3')
    for rid in rules:outcome(pg_db,bid,rid,good)
    outcome(pg_db,bid,rules[0],bad,'outdated');outcome(pg_db,bid,rules[1],bad,'prohibited')
    outcome(pg_db,bid,rules[0],partial)
    server(pg_db,'linux',os='linux');server(pg_db,'disabled',status='disabled')
    with api_client(pg_db,role='admin') as client:
        r=read(client,bid)
        assert r['summary'] | {'last_evaluated':None}=={'servers_in_scope':4,'compliant':1,'violated':1,'not_evaluated':2,'evaluated':3,'violations':2,'last_evaluated':None}
        assert r['items'][0]['server_id']==str(bad)
        assert r['items'][0]['critical']==2
        assert r['baseline']['servers_compliant']==1
        checks=read(client,bid,f'servers/{partial}/checks')['items']
        assert {x['status'] for x in checks}=={'compliant','not_evaluated'}
        assert checks[0]['status']=='not_evaluated'
        assert all(x['notes']=='Install the approved package' for x in checks)
        assert read(client,bid,status='compliant')['total']==1
        assert read(client,bid,status='not_evaluated')['total']==2
        assert read(client,bid,q='192.168.8.2')['total']==1
        assert read(client,bid,q='nothing')['summary']['servers_in_scope']==4
        assert {x['rule_id']:x['compliant'] for x in r['rule_summary']}=={str(rules[0]):2,str(rules[1]):1}


def test_stale_results_and_scope_edits_never_look_compliant(pg_db):
    bid,rules=policy(pg_db,tags=['prod'])
    sid=server(pg_db,tags=['prod'])
    for rid in rules:outcome(pg_db,bid,rid,sid,age='2 days')
    other=server(pg_db,'outside',tags=['dev'])
    for rid in rules:outcome(pg_db,bid,rid,other,'missing')
    with api_client(pg_db,role='admin') as client:
        r=read(client,bid)
        assert r['summary']['servers_in_scope']==1
        assert r['summary']['not_evaluated']==1
        assert r['summary']['compliant']==r['summary']['violations']==0
        assert all(x['status']=='not_evaluated' for x in read(client,bid,f'servers/{sid}/checks')['items'])
        assert read(client,bid,'results')['items']==[]
        assert client.get(f'/api/v1/server-baselines/{bid}/servers/{other}/checks').status_code==404
        listed=client.get('/api/v1/server-baselines').json()['items'][0]
        assert listed['violations']==listed['servers_evaluated']==0


def test_server_visibility_applies_to_counts_rules_results_and_direct_links(pg_db):
    bid,rules=policy(pg_db)
    visible=server(pg_db,tags=['Team-A']);hidden=server(pg_db,'private',tags=['team-b'])
    for sid in (visible,hidden):
        for rid in rules:outcome(pg_db,bid,rid,sid,'missing')
    with api_client(pg_db,role='baseline-team',permissions=['servers.view'],role_scope_tags=('team-a',)) as client:
        r=read(client,bid)
        assert r['summary']['servers_in_scope']==1 and r['summary']['violations']==2
        assert r['items'][0]['server_id']==str(visible)
        assert all(x['violated']==1 for x in r['rule_summary'])
        assert len(read(client,bid,'results')['items'])==2
        assert client.get(f'/api/v1/server-baselines/{bid}/servers/{hidden}/checks').status_code==404
        baseline=client.get(f'/api/v1/server-baselines/{bid}').json()
        assert baseline['violations']==2 and baseline['servers_evaluated']==1
        assert client.post(f'/api/v1/server-baselines/{bid}/evaluate').status_code==403
    with api_client(pg_db,role='no-server-access',permissions=['devices.view']) as client:
        assert client.get(f'/api/v1/server-baselines/{bid}/coverage').status_code==403


def test_disabled_and_empty_policies_do_not_claim_compliance(pg_db):
    bid,rules=policy(pg_db);sid=server(pg_db)
    for rid in rules:outcome(pg_db,bid,rid,sid)
    pg_db.execute('DELETE FROM software_baseline_rules WHERE baseline_id=$1',bid)
    with api_client(pg_db,role='admin') as client:
        r=read(client,bid);assert r['summary']['compliant']==0 and r['summary']['not_evaluated']==1
        pg_db.execute('UPDATE software_baselines SET enabled=false WHERE id=$1',bid)
        r=read(client,bid);assert r['summary']['servers_in_scope']==0 and r['items']==[]


def test_numeric_ip_natural_names_and_pagination_precede_response(pg_db):
    bid,_=policy(pg_db)
    pg_db.execute("""INSERT INTO servers(display_name,hostname,primary_ip,os_type)
        SELECT 'server'||n,'server'||n,('192.168.8.'||n)::inet,'windows' FROM generate_series(1,205) n""")
    with api_client(pg_db,role='admin') as client:
        rows=[x for p in range(1,6) for x in read(client,bid,sort='server_name',page=p,page_size=50)['items']]
        assert [x['server_name'] for x in rows]==['server'+str(n) for n in range(1,206)]
        assert len({x['server_id'] for x in rows})==205
        assert read(client,bid,sort='primary_ip',order='desc')['items'][0]['primary_ip']=='192.168.8.205'
        empty=read(client,bid,page=99)
        assert empty['total']==205 and empty['items']==[]
        server(pg_db,'literal%_host','203.0.113.1')
        assert read(client,bid,q='%_')['total']==1
        assert client.get(f'/api/v1/server-baselines/{bid}/coverage',params={'sort':'server_name; drop table servers'}).status_code==422
        assert client.get(f'/api/v1/server-baselines/{uuid4()}/coverage').status_code==404
