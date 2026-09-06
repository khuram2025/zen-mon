#!/usr/bin/env python3
"""Run with the new server/poller installed and writes paused; rollback on failure."""
import asyncio
import json
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.core.crypto import encrypt_text, decrypt_secret, encrypt_config, decrypt_config

async def main():
    counts={}
    async with AsyncSessionLocal() as db:
        try:
            for table, fields in [('snmp_credentials',['community','v3_auth_passphrase','v3_priv_passphrase']),('devices',['snmp_community']),('discovery_jobs',['community'])]:
                rows=(await db.execute(text('SELECT id,'+','.join(fields)+' FROM '+table+' FOR UPDATE'))).mappings().all()
                count=0
                for row in rows:
                    for field in fields:
                        value=row[field]
                        if not value or value.startswith('enc:v1:'): continue
                        encrypted=encrypt_text(value)
                        assert decrypt_secret(encrypted)==value
                        await db.execute(text('UPDATE '+table+' SET '+field+'=:value WHERE id=:id'), {'id':row['id'],'value':encrypted})
                        count+=1
                counts[table]=count
            for key,field in [('auth.ldap','bind_password'),('auth.radius','secret')]:
                row=(await db.execute(text('SELECT value FROM system_settings WHERE key=:key FOR UPDATE'),{'key':key})).first()
                if not row: continue
                config=dict(row[0]); value=config.get(field)
                if value and not value.startswith('enc:v1:'):
                    config[field]=encrypt_text(value)
                    assert decrypt_secret(config[field])==value
                    await db.execute(text('UPDATE system_settings SET value=CAST(:value AS jsonb) WHERE key=:key'),{'key':key,'value':json.dumps(config)})
                    counts[key]=1
            for table in ('notification_gateways', 'notification_channels'):
                rows=(await db.execute(text('SELECT id,config FROM '+table+' FOR UPDATE'))).mappings().all()
                counts[table]=0
                for row in rows:
                    if not row['config'] or '_encrypted_v1' in row['config']: continue
                    value=encrypt_config(row['config'])
                    assert decrypt_config(value)==row['config']
                    await db.execute(text('UPDATE '+table+' SET config=CAST(:value AS jsonb) WHERE id=:id'), {'id':row['id'],'value':json.dumps(value)})
                    counts[table]+=1
            for key in ('smtp', 'sms'):
                row=(await db.execute(text('SELECT value FROM system_settings WHERE key=:key FOR UPDATE'),{'key':key})).first()
                if row and row[0] and '_encrypted_v1' not in row[0]:
                    value=encrypt_config(row[0])
                    assert decrypt_config(value)==row[0]
                    await db.execute(text('UPDATE system_settings SET value=CAST(:value AS jsonb) WHERE key=:key'),{'key':key,'value':json.dumps(value)})
                    counts[key]=1
            await db.commit()
            print('Encrypted secret fields:',counts)
        except Exception:
            await db.rollback()
            raise
    await engine.dispose()
if __name__=='__main__': asyncio.run(main())
