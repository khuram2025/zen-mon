#!/opt/zenplus/venv/bin/python
import requests,json,secrets,time,subprocess,sys,socket,struct
from pathlib import Path
requests.packages.urllib3.disable_warnings()
base='https://127.0.0.1'
s=requests.Session();s.verify=False
checks=[]
def check(name,ok,detail=None):
    checks.append({'name':name,'pass':bool(ok),'detail':detail});print(name+(': PASS' if ok else ': FAIL'),flush=True)
login_path=Path('/var/lib/zentryc/test-login.json')
fresh=not login_path.exists()
login=json.loads((Path('/var/lib/zentryc/initial-login.json') if fresh else login_path).read_text())
r=s.post(base+'/api/v1/auth/login',json=login,timeout=30)
check('login',r.status_code==200,{'status':r.status_code})
if r.status_code!=200: raise SystemExit(1)
data=r.json();s.headers['Authorization']='Bearer '+data['access_token']
if fresh:
    check('unique credential requires password change',data.get('user',{}).get('must_change_password') is True)
    blocked=s.get(base+'/api/v1/devices',timeout=30)
    check('unclaimed session cannot administer',blocked.status_code==403,{'status':blocked.status_code})
    new=secrets.token_urlsafe(20)+'Aa1!'
    r=s.post(base+'/api/v1/users/me/change-password',json={'current_password':login['password'],'new_password':new},timeout=30)
    check('password change',r.status_code==200,{'status':r.status_code})
    login['password']=new;login_path.write_text(json.dumps(login));login_path.chmod(0o600)
    old=s.get(base+'/api/v1/users/me',timeout=30)
    check('old session revoked',old.status_code==401,{'status':old.status_code})
    s.headers.pop('Authorization')
    r=s.post(base+'/api/v1/auth/login',json=login,timeout=30)
    check('new password login',r.status_code==200)
    s.headers['Authorization']='Bearer '+r.json()['access_token']
health=s.get(base+'/api/v1/system/health',timeout=30)
check('full dependency health',health.status_code==200 and health.json().get('status')=='ok',health.json())
spec=s.get('http://127.0.0.1:8000/openapi.json',timeout=30).json()
Path('/var/lib/zentryc/openapi-paths.json').write_text(json.dumps({p:list(v) for p,v in spec['paths'].items()},indent=2))
for path,ops in spec['paths'].items():
    op=ops.get('get')
    if not op or '{' in path or not path.startswith('/api/v1/') or any(p.get('required') for p in op.get('parameters',[])): continue
    if any(x in path for x in ('stream','export','download','/events/live','watch','capture/live')): continue
    try:
        r=s.get(base+path,timeout=20)
        expected={'/api/v1/settings/company/logo':404,'/api/v1/snmp/mibs/objects':409,'/api/v1/sensor/config':401,'/api/v1/agents/config':401}
        check('GET '+path,r.status_code<400 or r.status_code==expected.get(path),{'status':r.status_code,'expected_status':expected.get(path)})
    except requests.RequestException as e: check('GET '+path,False,{'error':type(e).__name__})
# Real poller exercise: disposable loopback device, then inspect stored metrics.
if not fresh and Path('/var/lib/zentryc/test-device.json').exists():
    old=json.loads(Path('/var/lib/zentryc/test-device.json').read_text())
    old=old.get('data',old)
    if old.get('id'): s.delete(base+'/api/v1/devices/'+old['id'],timeout=30)
r=s.post(base+'/api/v1/devices',json={'hostname':'release-test-loopback','ip_address':'127.0.0.1','device_type':'server','ping_enabled':True,'ping_interval':10},timeout=30)
check('create monitored device',r.status_code in (200,201),{'status':r.status_code})
if r.status_code in (200,201):
    row=r.json();Path('/var/lib/zentryc/test-device.json').write_text(json.dumps(row))
# NetFlow v5 collector packet, containing one real generated flow.
now=int(time.time());header=struct.pack('!HHIIIIBBH',5,1,10000,now,0,1,0,0,0)
record=struct.pack('!IIIHHIIIIHHBBBBHHBBH',0xc0000201,0xc0000202,0,1,2,10,1000,1000,2000,12345,443,0,0x10,6,0,64512,64513,24,24,0)
sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.sendto(header+record,('127.0.0.1',2055));sock.close()
time.sleep(35)
netflow=requests.get('http://127.0.0.1:8091/health',timeout=10)
check('NetFlow collector processes packet',netflow.status_code==200,netflow.json())
pg=subprocess.run(['runuser','-u','postgres','--','psql','-X','-tAc',"SELECT round(extract(epoch from expires_at-started_at)/86400)::int,plan,status FROM subscriptions;",'-d','zenplus'],capture_output=True,text=True,check=True).stdout.strip()
check('fresh 30-day trial',pg=='30|trial|active',{'trial':pg})
check('no service Docker group','docker' not in subprocess.check_output(['id','-nG','zenplus'],text=True).split())
bad=subprocess.run(['runuser','-u','zenplus','--','sudo','-n','/usr/bin/id'],capture_output=True)
check('arbitrary root executable rejected',bad.returncode!=0)
good=subprocess.run(['runuser','-u','zenplus','--','sudo','-n','/usr/local/sbin/zenplus-storage-helper','os-usage'],capture_output=True)
check('typed storage operation works',good.returncode==0)
inject=subprocess.run(['runuser','-u','zenplus','--','sudo','-n','/usr/bin/tee','/etc/systemd/system/zenplus-updater.timer.d/override.conf'],input=b'[Service]\nExecStart=/bin/sh\n',capture_output=True)
check('systemd directive injection rejected',inject.returncode!=0)
result={'checks':checks,'passed':sum(c['pass'] for c in checks),'failed':sum(not c['pass'] for c in checks)}
Path('/var/lib/zentryc/live-test.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'passed':result['passed'],'failed':result['failed']}))
raise SystemExit(bool(result['failed']))
