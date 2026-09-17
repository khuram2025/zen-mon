"""Durable, bounded backup execution with leases and per-device serialization."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import text
from app.services import ncm_events


async def enqueue(db,device_id,*,actor=None,trigger='manual',config_types=None):
    row=(await db.execute(text('SELECT enabled,credential_id,config_types FROM device_ncm WHERE device_id=:id FOR UPDATE'),{'id':device_id})).first()
    if not row or not row.enabled or not row.credential_id:
        raise ValueError('Device needs an enabled NCM connection profile')
    types=config_types or row.config_types
    if not types or any(t not in ('running','startup') for t in types):
        raise ValueError('Unsupported configuration type')
    job=(await db.execute(text('''INSERT INTO ncm_jobs(device_id,config_types,trigger,actor_id,actor_name)
        VALUES(:device,:types,:trigger,:actor,:name)
        ON CONFLICT(device_id) WHERE status IN ('queued','running') DO NOTHING RETURNING id'''),
        {'device':device_id,'types':types,'trigger':trigger,'actor':getattr(actor,'id',None),
         'name':getattr(actor,'username','scheduler')})).first()
    if job:
        await ncm_events.audit(db,actor,'job.queued',device_id=device_id,resource_id=job.id,
                               metadata={'config_types':types,'trigger':trigger})
        return {'job_id':str(job.id),'queued':True}
    existing=(await db.execute(text("SELECT id FROM ncm_jobs WHERE device_id=:id AND status IN ('queued','running')"),{'id':device_id})).first()
    return {'job_id':str(existing.id),'queued':False}


async def claim(db):
    token=uuid4()
    row=(await db.execute(text('''WITH candidate AS (
        SELECT id FROM ncm_jobs WHERE (status='queued' AND available_at<=now())
           OR (status='running' AND lease_until<now())
        ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
        UPDATE ncm_jobs j SET status='running',attempts=attempts+1,
          started_at=COALESCE(started_at,now()),lease_until=now()+interval '2 minutes',lease_token=:token
        FROM candidate c WHERE j.id=c.id RETURNING j.*'''),{'token':token})).first()
    await db.commit()
    return row


async def heartbeat(sessions,job,stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(),timeout=20)
        except asyncio.TimeoutError:
            async with sessions() as db:
                await db.execute(text("UPDATE ncm_jobs SET lease_until=now()+interval '2 minutes' WHERE id=:id AND lease_token=:token AND status='running'"),{'id':job.id,'token':job.lease_token})
                await db.commit()


async def execute(sessions,job):
    from app.api.v1.ncm import _do_fetch
    stop=asyncio.Event()
    pulse=asyncio.create_task(heartbeat(sessions,job,stop))
    actor=SimpleNamespace(id=job.actor_id,username=job.actor_name)
    results=[]
    status='success'
    error=None
    try:
        if job.attempts>3:
            status,error='failed','Retry budget exhausted after worker interruption'
        else:
            for config_type in job.config_types:
                async with sessions() as db:
                    current=(await db.execute(text('SELECT cancel_requested,lease_token FROM ncm_jobs WHERE id=:id'),{'id':job.id})).first()
                    if not current or current.lease_token!=job.lease_token:
                        return
                    if current.cancel_requested:
                        status='cancelled'
                        break
                    result=await _do_fetch(db,job.device_id,actor=actor,trigger=job.trigger,config_type=config_type)
                    results.append({'config_type':config_type,**result})
                    await db.execute(text('UPDATE ncm_jobs SET results=CAST(:results AS jsonb) WHERE id=:id AND lease_token=:token'),{'results':json.dumps(results),'id':job.id,'token':job.lease_token})
                    await db.commit()
    except Exception:
        status='failed' if job.attempts>=3 else 'queued'
        error='Backup attempt failed; see device backup history'
    finally:
        stop.set()
        await pulse
    async with sessions() as db:
        updated=(await db.execute(text('''UPDATE ncm_jobs SET status=:status,error_code=:error,
            finished_at=CASE WHEN :status='queued' THEN NULL ELSE now() END,
            available_at=now()+make_interval(secs=>:delay),lease_until=NULL,lease_token=NULL,
            results=CAST(:results AS jsonb)
            WHERE id=:id AND lease_token=:token RETURNING id'''),
            {'status':status,'error':error,'delay':60*job.attempts,'results':json.dumps(results),
             'id':job.id,'token':job.lease_token})).first()
        if updated:
            await ncm_events.audit(db,actor,'job.'+status,device_id=job.device_id,resource_id=job.id)
        await db.commit()


async def worker(sessions,*,concurrency=4,once=False):
    async def slot():
        while True:
            async with sessions() as db:
                job=await claim(db)
            if job:
                await execute(sessions,job)
            elif once:
                return
            else:
                await asyncio.sleep(2)
    await asyncio.gather(*(slot() for _ in range(max(1,min(concurrency,16)))))
