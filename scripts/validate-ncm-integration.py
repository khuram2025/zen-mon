"""Run NCM tests in an explicitly isolated, unprivileged PostgreSQL fixture."""
from pathlib import Path
import os
import secrets
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
assert root.parent==Path('/tmp') and root.name.startswith('zenplus-ncm-integration-')
assert (root/'pgsocket').is_dir() and not (root/'pgsocket').is_symlink()
os.environ.update(DATABASE_URL=f'postgresql+asyncpg://ncm_fixture@localhost:15433/ncm_fixture?host={root}/pgsocket',
    SNMP_ENC_KEY=secrets.token_hex(32),ZENPLUS_NCM_INTEGRATION='1',ZENPLUS_API='http://127.0.0.1:1',
    ZENPLUS_DIR=str(root))
sys.path.insert(0,str(root/'server'))
psql=['/usr/lib/postgresql/16/bin/psql','-X','-v','ON_ERROR_STOP=1','-h',str(root/'pgsocket'),'-p','15433','-U','ncm_fixture','-d','ncm_fixture']
base='''CREATE TABLE IF NOT EXISTS devices(id uuid PRIMARY KEY,hostname text,ip_address inet,device_type text,tags jsonb,vendor text,location text);
CREATE TABLE IF NOT EXISTS notification_channels(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),type text,config jsonb,gateway_id uuid,enabled bool);
CREATE TABLE IF NOT EXISTS alerts(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),device_id uuid,rule_id uuid,status text,severity text,message text,triggered_at timestamptz,metadata jsonb);
'''
subprocess.run(psql,input=base,text=True,check=True,stdout=subprocess.DEVNULL)
for pattern in ('023-ncm','024-ncm','025-ncm','026-ncm','027-ncm','028-ncm','029-ncm','117-ncm','117-ncm','118-ncm','118-ncm'):
    filename=next((root/'scripts').glob('migrate-'+pattern+'*.sql'))
    subprocess.run(psql+['-f',str(filename)],check=True,stdout=subprocess.DEVNULL)
os.chdir(root/'server')
import pytest
raise SystemExit(pytest.main(['tests/integration/test_ncm_contracts.py','tests/test_ncm_assurance.py','-q','--disable-warnings','--tb=short',*sys.argv[1:]]))
