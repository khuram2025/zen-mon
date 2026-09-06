"""Run against a controller DB; all fixtures are rolled back before returning."""
import asyncio
from uuid import uuid4
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.services.sensor_overview import DEVICE_SQL, SERVICE_SQL, sensor_measurements

async def main():
    async with AsyncSessionLocal() as db:
        sid,gid,direct,inherited,overridden,default,service=[uuid4() for _ in range(7)]
        try:
            await db.execute(text("INSERT INTO sensors(id,name,status) VALUES(:id,:name,'offline')"),{'id':sid,'name':'overview-test-'+str(sid)})
            await db.execute(text("INSERT INTO device_groups(id,name) VALUES(:id,:name)"),{'id':gid,'name':'overview-test-'+str(gid)})
            for index,did in enumerate([direct,inherited,overridden,default]):
                await db.execute(text("INSERT INTO devices(id,hostname,ip_address,group_id,default_sensor_id) VALUES(:id,:name,:ip,:group_id,:default_id)"),{'id':did,'name':'overview-test-'+str(did),'ip':'192.0.2.'+str(210+index),'group_id':gid if did in [inherited,overridden] else None,'default_id':sid if did==default else None})
            await db.execute(text("INSERT INTO service_checks(id,name,check_type,target_host,default_sensor_id) VALUES(:id,:name,'icmp','192.0.2.215',:sid)"),{'id':service,'name':'overview-test-'+str(service),'sid':sid})
            await db.execute(text("INSERT INTO sensor_assignments(sensor_id,target_type,target_id) VALUES(:sid,'device',:did),(:sid,'group',:gid)"),{'sid':sid,'did':direct,'gid':gid})
            await db.execute(text("INSERT INTO monitoring_policies(target_type,target_id,controller_enabled) VALUES('device',:did,true)"),{'did':overridden})
            rows=(await db.execute(text(DEVICE_SQL),{'sid':sid})).mappings().all()
            assert {row['id'] for row in rows}=={direct,inherited,default}, rows
            assert {row['assignment_source'] for row in rows}=={'Direct','Device group','Default sensor'}
            rows=(await db.execute(text(SERVICE_SQL),{'sid':sid})).mappings().all()
            assert [row['id'] for row in rows]==[service]
            await db.execute(text("INSERT INTO monitoring_policies(target_type,target_id,controller_enabled) VALUES('service_check',:id,true)"),{'id':service})
            assert not (await db.execute(text(SERVICE_SQL),{'sid':sid})).mappings().all()
            assert sensor_measurements(sid,[{'id':direct}],[{'id':service}]) == {'ping':{},'snmp':{},'service':{}}
            print('PASS: direct, group, default, offline inventory, policy overrides and real ClickHouse query execution')
        finally:
            await db.rollback()
    await engine.dispose()
if __name__=='__main__':asyncio.run(main())
