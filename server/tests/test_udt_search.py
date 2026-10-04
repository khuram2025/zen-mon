"""Search contracts, with database cases isolated in disposable fixtures."""
import asyncio
import os
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.services.udt_service import endpoint_search_condition


def test_ipv4_is_not_a_mac_and_ipv6_is_canonical():
    for value, expected in [('192.168.100.101', '192.168.100.101'),
                            ('2001:0db8:0:0:0:0:0:1', '2001:db8::1')]:
        params = {}
        condition = endpoint_search_condition(value, params, True)
        assert params == {'search_ip': expected}
        assert 'search_mac' not in condition and 'udt_ip_history' in condition


def test_user_search_is_permission_gated_and_wildcards_are_literal():
    params = {}
    condition = endpoint_search_condition('alice_%', params, False)
    assert 'user_name' not in condition and 'udt_user_logins' not in condition
    assert params['q'] == r'%alice\_\%%'
    assert 'udt_user_logins' in endpoint_search_condition('alice', {}, True)
    assert endpoint_search_condition('   ', {}, True) is None


@pytest.mark.parametrize('value', ['8C:16:45:2F:0A:A1', '8c-16-45-2f-0a-a1',
                                  '8c16.452f.0aa1', '8c16452f0aa1'])
def test_mac_formats(value):
    params = {}
    endpoint_search_condition(value, params, False)
    assert params['search_mac'] == '8c:16:45:2f:0a:a1'


def test_search_database_results_and_permissions():
    dsn = os.environ.get('UDT_TEST_DSN')
    if not dsn:
        pytest.skip('UDT_TEST_DSN disposable fixture required')

    async def run():
        from app.api.v1.udt import list_endpoints
        engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://', 1))
        async with engine.begin() as conn:
            for ddl in [
                '''CREATE TEMP TABLE udt_endpoints(id uuid, mac macaddr, vendor text, hostname text,
                   ip_address inet, endpoint_type text, type_source text, is_randomized boolean DEFAULT false,
                   is_watched boolean DEFAULT false, authorized boolean DEFAULT true, ignored boolean DEFAULT false,
                   user_name text, user_domain text, device_id uuid, first_seen timestamptz DEFAULT now(),
                   last_seen timestamptz DEFAULT now()) ON COMMIT DROP''',
                'CREATE TEMP TABLE udt_ip_history(endpoint_id uuid, ip inet) ON COMMIT DROP',
                'CREATE TEMP TABLE udt_ip_evidence(endpoint_id uuid, ip inet) ON COMMIT DROP',
                'CREATE TEMP TABLE udt_user_logins(endpoint_id uuid, user_name text, user_domain text) ON COMMIT DROP',
                '''CREATE TEMP TABLE udt_endpoint_locations(endpoint_id uuid, device_id uuid, if_index int,
                   vlan_id int, is_direct boolean, active boolean, last_seen timestamptz) ON COMMIT DROP''',
                'CREATE TEMP TABLE devices(id uuid, hostname text) ON COMMIT DROP',
                'CREATE TEMP TABLE device_interfaces(device_id uuid, if_index int, if_name text) ON COMMIT DROP',
                '''INSERT INTO udt_endpoints(id,mac,vendor,hostname,ip_address,endpoint_type,type_source,user_name,user_domain) VALUES
                   ('00000000-0000-0000-0000-000000000001','8c:16:45:2f:0a:a1','LCFC(Hefei)','test-client','192.168.100.101','unknown','auto','Alice','EXAMPLE'),
                   ('00000000-0000-0000-0000-000000000002','00:11:22:33:44:55','Dell Inc.','other-client','192.168.100.10','unknown','auto',NULL,NULL)''',
                "INSERT INTO udt_ip_history VALUES ('00000000-0000-0000-0000-000000000001','192.0.2.99')",
                "INSERT INTO udt_ip_evidence VALUES ('00000000-0000-0000-0000-000000000001','2001:db8::1')",
                "INSERT INTO udt_user_logins VALUES ('00000000-0000-0000-0000-000000000002','HistoricalBob','OLD')",
            ]:
                await conn.execute(text(ddl))
            async with AsyncSession(bind=conn) as db:
                async def search(q, see_users=True, **filters):
                    async def permitted(*args): return see_users
                    with patch('app.api.v1.udt._can_see_users', permitted):
                        return await list_endpoints(q=q, status=filters.get('status'), endpoint_type=None,
                            vlan=None, device_id=None, flag=None, include_ignored=False,
                            sort='last_seen', order='desc', skip=0, limit=50, db=db, user=None)
                for query in ['192.168.100.101','192.0.2.99','2001:0db8:0:0:0:0:0:1',
                              '8C:16:45:2F:0A:A1','8c-16-45-2f-0a-a1','8c16.452f.0aa1',
                              '8c16452f0aa1','8c:16','8c1645','lcfc','Alice','EXAMPLE\\Alice',
                              'alice@example','test-client']:
                    result = await search(query)
                    assert result['meta']['total'] == 1, (query, result)
                    assert result['data'][0]['id'].endswith('0001'), query
                assert (await search('HistoricalBob'))['data'][0]['id'].endswith('0002')
                assert (await search('old\\HistoricalBob'))['meta']['total'] == 1
                for query in ['Alice','EXAMPLE','HistoricalBob','OLD']:
                    assert (await search(query, False))['meta']['total'] == 0
                public = await search('LCFC', False)
                assert public['data'][0]['user_name'] is None and public['data'][0]['user_domain'] is None
                for query in ['192.168.100.1','does-not-exist','%', '_', "' OR 1=1 --"]:
                    assert (await search(query))['meta']['total'] == 0, query
                assert (await search('   '))['meta']['total'] == 2
                assert (await search('LCFC', status='inactive'))['meta']['total'] == 0
        await engine.dispose()
    asyncio.run(run())
