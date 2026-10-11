#!/opt/zenplus/venv/bin/python
import json,requests,subprocess,time,hashlib
from pathlib import Path
requests.packages.urllib3.disable_warnings()
base='https://127.0.0.1';s=requests.Session();s.verify=False
login=json.loads(Path('/var/lib/zentryc/test-login.json').read_text())
for _ in range(30):
    r=s.post(base+'/api/v1/auth/login',json=login,timeout=10)
    if r.status_code==200:break
    time.sleep(1)
r.raise_for_status();s.headers['Authorization']='Bearer '+r.json()['access_token']
results=[]
def check(name,ok,detail=None):
    results.append({'name':name,'pass':bool(ok),'detail':detail});print(name+(': PASS' if ok else ': FAIL'),flush=True)
r=s.put(base+'/api/v1/system/security/tls/config',json={'https_enabled':True,'redirect_http':True,'hsts_enabled':False,'min_tls_version':'1.2'},timeout=30)
check('TLS configuration through restricted helper',r.status_code==200,{'status':r.status_code})
r=s.get(base+'/api/v1/system/security/tls',timeout=30)
check('TLS state matches active configuration',r.status_code==200 and r.json()['settings']['https_enabled'] and r.json()['applied']['https']=='on')
r=requests.get('http://127.0.0.1/api/v1/system/health',allow_redirects=False,timeout=10)
check('HTTP redirects to HTTPS',r.status_code==301 and r.headers.get('Location','').startswith('https://'))
r=s.get(base+'/.env',timeout=10)
check('credential files inaccessible via web',r.status_code==403)
policy={'web_restricted':True,'ssh_restricted':False,'allowed_cidrs':['127.0.0.1/32','::1/128']}
try:
    r=s.put(base+'/api/v1/system/security/access',json=policy,timeout=30)
    check('management allowlist applies',r.status_code==200,{'status':r.status_code})
    check('allowed management address works',s.get(base+'/api/v1/auth/me',timeout=10).status_code==200)
finally:
    r=s.put(base+'/api/v1/system/security/access',json={'web_restricted':False,'ssh_restricted':False,'allowed_cidrs':[]},timeout=30)
    check('management allowlist can be restored',r.status_code==200)
def sql(query):return subprocess.check_output(['runuser','-u','postgres','--','psql','-X','-tAc',query,'-d','zenplus'],text=True).strip()
expiry=sql('SELECT expires_at FROM subscriptions LIMIT 1;')
try:
    sql("UPDATE subscriptions SET expires_at=now()-interval '1 day';")
    r=s.get(base+'/api/v1/subscription',timeout=30)
    check('trial expiration is reported',r.status_code==200 and r.json()['status']=='expired' and r.json()['days_remaining']==0)
finally:
    sql(f"UPDATE subscriptions SET expires_at='{expiry}'::timestamptz,status='active';")
secureboot=list(Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'))
check('UEFI Secure Boot enabled',bool(secureboot) and secureboot[0].read_bytes()[4:5]==b'\x01')
check('runtime source is root-owned',Path('/opt/zenplus/server/app/main.py').stat().st_uid==0)
check('root dispatcher cannot be written by service',subprocess.run(['runuser','-u','zenplus','--','test','-w','/usr/local/libexec/zentryc-privileged']).returncode!=0)
result={'checks':results,'passed':sum(x['pass'] for x in results),'failed':sum(not x['pass'] for x in results)}
Path('/var/lib/zentryc/security-test.json').write_text(json.dumps(result,indent=2));print(json.dumps({'passed':result['passed'],'failed':result['failed']}))
raise SystemExit(bool(result['failed']))
