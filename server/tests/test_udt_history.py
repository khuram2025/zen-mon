"""Database regressions; temporary tables and rollback protect live records."""
import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.services.udt_history import address_history, address_periods, address_evidence


def test_history_summary_and_pagination_preserve_old_addresses():
    dsn = os.environ.get('UDT_TEST_DSN')
    if not dsn:
        pytest.skip('UDT_TEST_DSN not set')

    async def scenario():
        engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://', 1))
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                for query in [
                    '''CREATE TEMP TABLE udt_endpoints(id uuid, mac macaddr, hostname text) ON COMMIT DROP''',
                    '''CREATE TEMP TABLE device_interfaces(device_id uuid, if_index int, if_name text, if_descr text) ON COMMIT DROP''',
                    '''CREATE TEMP TABLE udt_ip_evidence(endpoint_id uuid, ip inet, reporting_device_id uuid, if_index int, source text, first_seen timestamptz, last_seen timestamptz, observation_count bigint, recent_sightings timestamptz[]) ON COMMIT DROP''',
                    'CREATE TEMP TABLE devices(id uuid, hostname text, ip_address inet) ON COMMIT DROP',
                    '''CREATE TEMP TABLE udt_ip_history(id bigserial, endpoint_id uuid, ip inet,
                       source text, active boolean, first_seen timestamptz, last_seen timestamptz,
                       reporting_device_id uuid) ON COMMIT DROP''',
                    '''INSERT INTO udt_ip_history(endpoint_id,ip,source,active,first_seen,last_seen)
                       SELECT '00000000-0000-0000-0000-000000000001', '192.0.2.10', 'arp', false,
                              NOW()-n*INTERVAL '1 hour', NOW()-n*INTERVAL '1 hour'
                       FROM generate_series(1,150) n''',
                    '''INSERT INTO udt_ip_history(endpoint_id,ip,source,active,first_seen,last_seen) VALUES
                       ('00000000-0000-0000-0000-000000000001','192.0.2.10','dhcp',true,NOW(),NOW()),
                       ('00000000-0000-0000-0000-000000000002','192.0.2.10','arp',true,NOW(),NOW()),
                       ('00000000-0000-0000-0000-000000000001','192.0.2.99','arp',false,NOW()-INTERVAL '100 days',NOW()-INTERVAL '99 days'),
                       ('00000000-0000-0000-0000-000000000001','2001:db8::1','nd',true,NOW()-INTERVAL '3 days',NOW()-INTERVAL '2 days')''',
                ]:
                    await conn.execute(text(query))
                async with AsyncSession(bind=conn) as db:
                    endpoint_id = '00000000-0000-0000-0000-000000000001'
                    summary = await address_history(db, endpoint_id)
                    assert len(summary) == 3
                    assert summary[0]['ip'] == '192.0.2.10'
                    assert summary[0]['period_count'] == 151
                    assert summary[0]['active_endpoint_count'] == 2
                    assert summary[0]['source'] == 'arp, dhcp'
                    assert not next(r for r in summary if r['ip'] == '2001:db8::1')['active']
                    assert any(r['ip'] == '192.0.2.99' for r in summary)
                    await db.execute(text("""
                        INSERT INTO udt_endpoints VALUES
                        ('00000000-0000-0000-0000-000000000001','00:11:22:33:44:55','Endpoint A'),
                        ('00000000-0000-0000-0000-000000000002','00:11:22:33:44:66','Endpoint B')
                    """))
                    legacy = await address_evidence(db, endpoint_id, '192.0.2.10', 0, 25)
                    assert legacy['meta']['total'] == 2
                    assert all(r['legacy'] and r['observation_count'] is None for r in legacy['data'])
                    await db.execute(text("""
                        INSERT INTO devices VALUES ('00000000-0000-0000-0000-000000000010',NULL,'192.0.2.254');
                    """))
                    await db.execute(text("""
                        UPDATE udt_ip_history SET reporting_device_id='00000000-0000-0000-0000-000000000010'
                    """))
                    await db.execute(text("""
                        INSERT INTO udt_ip_evidence VALUES
                        ('00000000-0000-0000-0000-000000000001','192.0.2.10','00000000-0000-0000-0000-000000000010',7,'dhcp',NOW(),NOW(),3,ARRAY[NOW(),NOW()-INTERVAL '5 minutes',NOW()-INTERVAL '10 minutes']),
                        ('00000000-0000-0000-0000-000000000002','192.0.2.10','00000000-0000-0000-0000-000000000010',8,'arp',NOW(),NOW(),500,ARRAY[NOW(),NOW()-INTERVAL '2 days',NOW()-INTERVAL '3 days'])
                    """))
                    evidence = await address_evidence(db, endpoint_id, '192.0.2.10', 0, 25)
                    assert evidence['meta']['total'] == 2  # No duplicate legacy fallback.
                    assert all(not r['legacy'] and r['reporter'] == '192.0.2.254' for r in evidence['data'])
                    assert next(r for r in evidence['data'] if r['if_index'] == 7)['repeated']
                    assert not next(r for r in evidence['data'] if r['if_index'] == 8)['repeated']
                    assert len((await address_evidence(db, endpoint_id, '192.0.2.10', 1, 1))['data']) == 1
                    assert (await address_evidence(db, endpoint_id, '192.0.2.200', 0, 25))['meta']['total'] == 0
                    pages = [await address_periods(db, endpoint_id, '192.0.2.10', offset, 25) for offset in range(0,151,25)]
                    assert all(p['meta']['total'] == 151 for p in pages)
                    assert len({r['id'] for p in pages for r in p['data']}) == 151
                    assert (await address_history(db, '00000000-0000-0000-0000-000000000099')) == []
            finally:
                await tx.rollback()
        await engine.dispose()
    asyncio.run(scenario())
