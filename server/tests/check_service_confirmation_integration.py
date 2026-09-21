"""Disposable PostgreSQL verification; use the dedicated Unix-socket fixture only."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.api.v1.sensor_api import _update_service_vantage_and_consensus

async def main():
 assert engine.url.database == 'zenplus' and 'zenplus-confirmation-integration-' in str(engine.url.query.get('host',''))
 async with AsyncSessionLocal() as db:
  sid, cid = uuid4(), uuid4()
  try:
   await db.execute(text("INSERT INTO sensors(id,name,status,version) VALUES(:sid,'confirmation fixture','online','1.23.15')"),{'sid':sid})
   await db.execute(text("INSERT INTO service_checks(id,name,check_type,target_host,status) VALUES(:cid,'confirmation fixture','http','example.test','up')"),{'cid':cid})
   defaults=(await db.execute(text('SELECT check_interval,timeout,retry_count,retry_delay_s,down_check_interval FROM service_checks WHERE id=:cid'),{'cid':cid})).one()
   assert tuple(defaults)==(60,10,3,10,30),defaults
   await db.execute(text("INSERT INTO sensor_assignments(sensor_id,target_type,target_id) VALUES(:sid,'service_check',:cid)"),{'sid':sid,'cid':cid})
   await db.execute(text("INSERT INTO monitoring_policies(target_type,target_id,controller_enabled) VALUES('service_check',:cid,FALSE)"),{'cid':cid})
   now=datetime.now(timezone.utc)-timedelta(seconds=20)
   async def sample(up,attempt,complete,offset):
    s={'is_up':up,'ts':now+timedelta(seconds=offset),'response_ms':5,'error':'' if up else 'fixture timeout','confirmation_attempt':attempt,'confirmation_complete':complete}
    event=await _update_service_vantage_and_consensus(str(cid),sid,s,db)
    row=(await db.execute(text('SELECT status,verification_attempt FROM service_checks WHERE id=:cid'),{'cid':cid})).one()
    return event,tuple(row)
   # Establish prior Up, then verify partial failures keep that state.
   await sample(True,1,True,0)
   event,state=await sample(False,1,False,1);assert event is None and state==('up',1),(event,state)
   event,state=await sample(False,2,False,2);assert event is None and state==('up',2)
   event,state=await sample(True,3,True,3);assert event is None and state==('up',0)
   await sample(False,1,False,4);await sample(False,2,False,5)
   event,state=await sample(False,3,True,6);assert state==('down',0) and event['new_status']=='down'
   event,state=await sample(True,1,True,7);assert state==('up',0) and event['new_status']=='up'
   # Late diagnostic retries must not resurrect progress or roll back recovery.
   event,state=await sample(False,2,False,5);assert event is None and state==('up',0)
   print('Disposable PostgreSQL: defaults, partial failures, glitch recovery, confirmed outage, recovery and delayed delivery passed')
  finally:
   await db.rollback()
 await engine.dispose()

if __name__ == "__main__":
 asyncio.run(main())
