"""Run against a migrated PostgreSQL database; all fixtures are rolled back."""
import asyncio
from uuid import uuid4
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.services.sensor_health_service import _suppress_unverifiable_service_alerts

async def main():
    async with AsyncSessionLocal() as db:
        sid, cid = str(uuid4()), str(uuid4())
        try:
            await db.execute(text("INSERT INTO sensors(id,name,status,version) VALUES(:sid,:name,'offline','1.23.5')"), {'sid':sid,'name':'parity-test-'+sid})
            await db.execute(text("INSERT INTO service_checks(id,name,check_type,target_host,target_url,status,workflow_steps) VALUES(:cid,:name,'http','example.test','https://example.test','up','[{\"name\":\"Health\",\"url\":\"https://example.test\"}]'::jsonb)"), {'cid':cid,'name':'parity-test-'+cid})
            await db.execute(text("INSERT INTO sensor_assignments(sensor_id,target_type,target_id) VALUES(:sid,'service_check',:cid)"), {'sid':sid,'cid':cid})
            await db.execute(text("INSERT INTO monitoring_policies(target_type,target_id,controller_enabled) VALUES('service_check',:cid,TRUE)"), {'cid':cid})
            await _suppress_unverifiable_service_alerts(db,sid,'Test')
            assert (await db.execute(text('SELECT status FROM service_checks WHERE id=:cid'),{'cid':cid})).scalar() == 'up', 'Offline secondary probe must not change controller status'
            await db.execute(text('UPDATE monitoring_policies SET controller_enabled=FALSE WHERE target_id=:cid'),{'cid':cid})
            await _suppress_unverifiable_service_alerts(db,sid,'Test')
            assert (await db.execute(text('SELECT status FROM service_checks WHERE id=:cid'),{'cid':cid})).scalar() == 'unknown', 'Offline sole probe must make workflow service unknown'
            print('Offline secondary/sole workflow probe checks passed; rolling back fixtures')
        finally:
            await db.rollback()
    await engine.dispose()

if __name__ == '__main__': asyncio.run(main())
