#!/opt/zenplus/venv/bin/python
"""Restore a disposable golden VM's backup and verify restored database values."""
import json,time,requests,subprocess
from pathlib import Path
requests.packages.urllib3.disable_warnings()
base='https://127.0.0.1';s=requests.Session();s.verify=False
r=s.post(base+'/api/v1/auth/login',json=json.loads(Path('/var/lib/zentryc/test-login.json').read_text()),timeout=30);r.raise_for_status()
s.headers['Authorization']='Bearer '+r.json()['access_token']
def sql(query):return subprocess.check_output(['runuser','-u','postgres','--','psql','-X','-tAc',query,'-d','zenplus'],text=True).strip()
before=sql("SELECT hostname FROM devices WHERE ip_address='127.0.0.1';")
assert before and before!='restore-test-marker'
bid=Path('/var/lib/zentryc/backup-test-id').read_text().strip()
sql("UPDATE devices SET hostname='restore-test-marker' WHERE ip_address='127.0.0.1';")
r=s.post(base+'/api/v1/system/storage/backups/'+bid+'/restore',json={'components':['postgres','clickhouse_schema','clickhouse_data']},timeout=30);r.raise_for_status()
for _ in range(150):
    time.sleep(2)
    # Restoring PostgreSQL can revoke sessions created after the backup. Check
    # the job directly, then sign in again to validate management access.
    raw=sql(f"SELECT json_build_object('last_restore_status',last_restore_status,'last_restore_error',last_restore_error) FROM storage_backups WHERE id='{bid}'::uuid;")
    data=json.loads(raw) if raw else {}
    if data.get('last_restore_status') in ('completed','failed'):break
after=sql("SELECT hostname FROM devices WHERE ip_address='127.0.0.1';")
r=s.post(base+'/api/v1/auth/login',json=json.loads(Path('/var/lib/zentryc/test-login.json').read_text()),timeout=30)
result={'restore_status':data.get('last_restore_status'),'error':data.get('last_restore_error'),'database_marker_restored':after==before,'login_after_restore':r.status_code==200}
Path('/var/lib/zentryc/restore-test.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result));raise SystemExit(not (data.get('last_restore_status')=='completed' and after==before and r.status_code==200))
