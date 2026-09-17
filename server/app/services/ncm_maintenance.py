"""Local NCM service entry point; uses the protected appliance environment."""
import argparse
import asyncio
import hashlib
import json
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.services.ncm_content import encrypt_content, read_content
from app.services import ncm_events


async def encrypt_legacy(db, batch_size=100):
    rows=(await db.execute(text('''SELECT id,content,content_hash FROM device_configs
        WHERE content_enc IS NULL ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED'''),
        {'limit':batch_size})).fetchall()
    for row in rows:
        # Preserve exact archive bytes and hashes; do not normalize migration.
        from app.core import crypto
        if hashlib.sha256(row.content.encode()).hexdigest()!=row.content_hash:
            raise ValueError('Legacy configuration integrity check failed')
        encrypted=crypto.encrypt(row.content)
        if encrypted is None or crypto.decrypt(encrypted)!=row.content:
            raise ValueError('Archive encryption verification failed')
        await db.execute(text("UPDATE device_configs SET content_enc=:enc,content='' WHERE id=:id"),
                         {'id':row.id,'enc':encrypted})
    if rows:
        await ncm_events.audit(db,None,'archive.encrypt',metadata={'count':len(rows)})
    await db.commit()
    return len(rows)


async def run(mode):
    try:
        if mode=='worker':
            from app.services.ncm_jobs import worker
            return await worker(AsyncSessionLocal)
        async with AsyncSessionLocal() as db:
            if mode=='encrypt':
                total=0
                while count:=await encrypt_legacy(db):
                    total+=count
                return {'encrypted':total}
            if mode=='verify':
                total=0
                result=await db.stream(text('SELECT content,content_enc,content_hash FROM device_configs'))
                async for row in result:
                    read_content(row)
                    total+=1
                return {'verified':total}
            if mode=='deliver':
                return await ncm_events.deliver_due(db)
            from app.api.v1.ncm import _run_scheduled
            return await _run_scheduled(db)
    finally:
        await engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['schedule','deliver','encrypt','verify','worker'])
    args=parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.mode))))
    except Exception:
        # Never log driver, SQL parameter or provider exceptions with secrets.
        print('NCM maintenance failed; check database, key and service configuration')
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
