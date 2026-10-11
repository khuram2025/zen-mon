#!/opt/zenplus/venv/bin/python
"""Initialize a clean customer appliance without network downloads."""
import os,sys,json,secrets,subprocess,time,re,socket
from pathlib import Path
from passlib.context import CryptContext

state=Path('/var/lib/zentryc')
state.mkdir(mode=0o700,parents=True,exist_ok=True)
state.chmod(0o700)
sentinel=state/'initialized.json'
if sentinel.exists(): raise SystemExit(0)
os.umask(0o077)
def run(args,**kw):
    p=subprocess.run(args,text=True,capture_output=True,**kw)
    if p.returncode: raise RuntimeError(f'{args[0]} failed: '+p.stderr[-3000:])
    return p.stdout
def sql(query):
    return run(['runuser','-u','postgres','--','psql','-X','-q','-v','ON_ERROR_STOP=1','-d','zenplus'],input=query)
def write(path,text,mode=0o600):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);p.chmod(mode)
for _ in range(120):
    if subprocess.run(['pg_isready','-q'],stdout=subprocess.DEVNULL).returncode==0: break
    time.sleep(1)
else: raise RuntimeError('PostgreSQL unavailable')
envpath=Path('/opt/zenplus/.env')
# A retry reuses generated secrets instead of invalidating partially initialized services.
if envpath.exists():
    env=dict(line.split('=',1) for line in envpath.read_text().splitlines() if '=' in line and not line.startswith('#'))
else:
    pg,ch,rd,jwt,snmp=[secrets.token_hex(32) for _ in range(5)]
    env={'POSTGRES_PASSWORD':pg,'DB_PASSWORD':pg,'CLICKHOUSE_PASSWORD':ch,'REDIS_PASSWORD':rd,'JWT_SECRET':jwt,'SNMP_ENC_KEY':snmp,
         'API_HOST':'127.0.0.1','API_PORT':'8000','DATABASE_URL':f'postgresql+asyncpg://zenplus:{pg}@127.0.0.1:5432/zenplus',
         'CLICKHOUSE_HOST':'127.0.0.1','CLICKHOUSE_PORT':'9000','CLICKHOUSE_HTTP_PORT':'8123','CLICKHOUSE_DB':'zenplus','CLICKHOUSE_USER':'default',
         'REDIS_URL':f'redis://:{rd}@127.0.0.1:6379/0','POLLER_ID':'poller-'+secrets.token_hex(8),
         'POSTGRES_HOST':'127.0.0.1','POSTGRES_PORT':'5432','POSTGRES_DB':'zenplus','POSTGRES_USER':'zenplus','REDIS_HOST':'127.0.0.1','REDIS_PORT':'6379'}
    write(envpath,'# Unique appliance secrets\n'+''.join(f'{k}={v}\n' for k,v in env.items()),0o640)
    run(['chown','root:zenplus',str(envpath)])
os.environ.update(env)
run(['make-ssl-cert','generate-default-snakeoil','--force-overwrite'])
pg=env['POSTGRES_PASSWORD']
run(['runuser','-u','postgres','--','psql','-X','-q','-v','ON_ERROR_STOP=1'],input=f"DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='zenplus') THEN CREATE ROLE zenplus LOGIN; END IF; END $$; ALTER ROLE zenplus PASSWORD '{pg}';")
if '1' not in run(['runuser','-u','postgres','--','psql','-X','-tAc',"SELECT 1 FROM pg_database WHERE datname='zenplus'"]):
    run(['runuser','-u','postgres','--','createdb','-O','zenplus','zenplus'])
if 't' not in sql("SELECT to_regclass('public.users') IS NOT NULL;"):
    base=Path('/opt/zenplus/scripts/init-postgres.sql').read_text()
    # The bootstrap account is locked while migrations run; there is no shared password.
    base=re.sub(r"(VALUES \('admin', 'admin@zenplus.local',\s*)'[^']+'",r"\1'!locked'",base)
    sql(base)
run(['runuser','-u','postgres','--','python3','/opt/zenplus/scripts/run-migrations.py','--scripts-dir','/opt/zenplus/scripts'])
sql('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zenplus; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO zenplus;')
redis=Path('/etc/redis/redis.conf')
text=re.sub(r'^user default .*\n','',redis.read_text(),flags=re.M)
text=re.sub(r'^requirepass .*\n','',text,flags=re.M)
write(redis,text+'\nrequirepass '+env['REDIS_PASSWORD']+'\n',0o640)
run(['chown','redis:redis',str(redis)]);run(['systemctl','restart','redis-server'])
run(['docker','compose','up','-d'],cwd='/opt/zenplus')
import requests
for _ in range(180):
    try:
        resp=requests.post('http://127.0.0.1:8123/',auth=('default',env['CLICKHOUSE_PASSWORD']),data='SELECT 1',timeout=3)
        if resp.status_code==200: break
    except requests.RequestException: pass
    time.sleep(1)
else: raise RuntimeError('ClickHouse unavailable')
run(['/opt/zenplus/venv/bin/python','/opt/zenplus/scripts/sync-schema.py','--json'])
claim=state/'initial-login.json'
if claim.exists(): login=json.loads(claim.read_text())
else:
    login={'username':'admin','password':secrets.token_urlsafe(18)}
    write(claim,json.dumps(login))
hashed=CryptContext(schemes=['bcrypt'],deprecated='auto').hash(login['password'])
sql(f"UPDATE users SET password_hash='{hashed}', must_change_password=TRUE, password_changed_at=NULL, last_login=NULL WHERE username='admin'; DELETE FROM subscriptions; INSERT INTO subscriptions(plan,status,started_at,expires_at,max_devices,max_service_checks,max_users,license_key,activated_by) VALUES('trial','active',now(),now()+interval '30 days',50,20,5,NULL,NULL);")
write('/opt/zenplus/updater/config/subscription.json','{}\n');run(['chown','zenplus:zenplus','/opt/zenplus/updater/config/subscription.json'])
run(['ssh-keygen','-A'])
serial='ZT-VM-'+secrets.token_hex(8).upper()
run(['hostnamectl','set-hostname','zentryc-'+serial[-8:].lower()])
version=Path('/opt/zenplus/.version').read_text().splitlines()[0].strip()
write('/etc/zentryc/identity.json',json.dumps({'serial':serial,'product':'ZenPlus','version':version}),0o644)
tls=Path('/etc/zenplus/tls');tls.mkdir(parents=True,exist_ok=True)
run(['openssl','req','-x509','-nodes','-newkey','rsa:3072','-days','365','-keyout',str(tls/'server.key'),'-out',str(tls/'server.crt'),'-subj','/CN='+serial+'.local','-addext','subjectAltName=DNS:'+serial+'.local,DNS:'+socket.gethostname()])
(tls/'server.key').chmod(0o600);(tls/'server.crt').chmod(0o644)
write(tls/'fullchain.pem',(tls/'server.crt').read_text(),0o644)
write('/var/lib/zenplus/.initialized','initialized\n',0o644)
write(sentinel,json.dumps({'serial':serial,'initialized_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'version':version}))
print('Zentryc ZenPlus initialization completed; trial active for 30 days.')
