#!/opt/zenplus/venv/bin/python
"""Exercise real appliance workflows. Run only on a disposable acceptance VM."""
import hashlib,json,time,requests,subprocess
from pathlib import Path
requests.packages.urllib3.disable_warnings()
base='https://127.0.0.1'
s=requests.Session();s.verify=False
login=json.loads(Path('/var/lib/zentryc/test-login.json').read_text())
r=s.post(base+'/api/v1/auth/login',json=login,timeout=30);r.raise_for_status()
s.headers['Authorization']='Bearer '+r.json()['access_token']
checks=[]
def check(name,ok,detail=None):
    checks.append({'name':name,'pass':bool(ok),'detail':detail});print(name+(': PASS' if ok else ': FAIL'),flush=True)
def get(path): return s.get(base+path,timeout=30)
config=get('/api/v1/system/update-status').json()
r=s.put(base+'/api/v1/system/update-config',json={'auto_update':False,'check_interval_hours':6,'maintenance_window_start':'02:00','maintenance_window_end':'05:00'},timeout=30)
check('typed timer configuration through HTTPS',r.status_code==200 and r.json().get('status')!='partial',{'status':r.status_code,'result':r.json()})
r=s.post(base+'/api/v1/agent-fleet/packages/publish',json={},timeout=30)
check('agent package registration',r.status_code==200)
r=get('/api/v1/agents/packages/windows/latest?arch=amd64&flavor=full')
check('offline Windows agent download',r.status_code==200 and hashlib.sha256(r.content).hexdigest()=='277aeed0ab941fe4c1f51720a484e55d9d6495287d810eadb5cd24a26148d4b6',{'status':r.status_code,'bytes':len(r.content)})
r=s.post(base+'/api/v1/support/bundles',json={'issue_summary':'Disposable appliance acceptance'},timeout=30)
check('support job enqueued',r.status_code==202,{'status':r.status_code})
if r.status_code==202:
    ident=r.json()['id']
    for _ in range(120):
        data=get('/api/v1/support/bundles/'+ident).json()
        if data.get('status') in ('ready','failed'):break
        time.sleep(2)
    check('support bundle generated',data.get('status')=='ready',{'status':data.get('status'),'error':data.get('error'),'collector_failures':data.get('collector_failures')})
    if data.get('status')=='ready':
        r=get('/api/v1/support/bundles/'+ident+'/download')
        check('support download integrity',r.status_code==200 and hashlib.sha256(r.content).hexdigest()==data['sha256'])
r=s.post(base+'/api/v1/system/storage/backups',json={'include_clickhouse':True,'note':'Disposable release acceptance'},timeout=30)
check('full data backup enqueued',r.status_code==200)
if r.status_code==200:
    bid=r.json()['id']
    for _ in range(120):
        rows=get('/api/v1/system/storage/backups').json()
        data=next((x for x in rows if x['id']==bid),{})
        if data.get('status') in ('completed','failed'):break
        time.sleep(2)
    check('full backup completed',data.get('status')=='completed',{'status':data.get('status'),'error':data.get('error')})
    if data.get('status')=='completed':
        r=get('/api/v1/system/storage/backups/'+bid+'/download')
        check('backup download',r.status_code==200 and len(r.content)>1000,{'bytes':len(r.content)})
        Path('/var/lib/zentryc/backup-test-id').write_text(bid)
check('application services survived cold boot',all(subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0 for u in ['zenplus-api','zenplus-poller','zenplus-netflow-collector','zenplus-ncm-worker','nginx']))
result={'checks':checks,'passed':sum(c['pass'] for c in checks),'failed':sum(not c['pass'] for c in checks)}
Path('/var/lib/zentryc/integration-test.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'passed':result['passed'],'failed':result['failed']}))
raise SystemExit(bool(result['failed']))
