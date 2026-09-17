"""Apply this approved dev candidate with the installed updater's normal engine."""
from pathlib import Path
import hashlib, json, logging, os, shutil, subprocess, sys, tarfile
from datetime import datetime, timezone

os.umask(0o077)
sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/zenplus')
from updater.config import load_config
from updater.crypto import verify_manifest, verify_checksums
from updater.executor import execute_manifest, rollback_manifest
from updater.lockfile import UpdateLock
from updater.version_policy import validate_manifest_transition
from updater.schema_gate import sync_and_verify
from updater.history import add_record

root = Path('/tmp/zenplus-network-integration-20260915')
installed = Path('/opt/zenplus')
package = root/'candidate/update-1.23.12.zup'
expected = '0ad09db0f4de39db291c26e8e962d68103f992f05966a2f82f8c44649e4df4e1'
assert os.getuid() == 0
assert hashlib.file_digest(package.open('rb'), 'sha256').hexdigest() == expected
assert (installed/'.version').read_text().splitlines()[0] == '1.23.11'
assert shutil.disk_usage(installed).free > 1500 * 1024 * 1024
extract = Path('/tmp/zenplus-updates/network-dev-1.23.12')
assert not extract.exists(), 'Inspect previous attempt before applying'
extract.mkdir(parents=True, mode=0o700)
with tarfile.open(package, 'r:gz') as tar:
    for member in tar.getmembers():
        assert member.isfile() or member.isdir()
        assert not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
    tar.extractall(extract, filter='data')
cfg = load_config()
manifest = verify_manifest(str(extract/'manifest.json'), str(extract/'manifest.json.sig'),
    cfg.security.public_key_path, max_age_days=cfg.security.max_manifest_age_days)
assert not verify_checksums(str(extract/'checksums.sha256'), str(extract))
validate_manifest_transition(manifest, {'version':'1.23.12', 'min_version':manifest.get('min_version')},
    '1.23.11', (extract/'code/.version').read_text().splitlines()[0])
backup = installed/'updater/backups/network-dev-20260915'
assert not backup.exists(), 'Inspect previous backup before applying'
backup.mkdir(mode=0o700)
cfg.update.backup_dir = str(backup/'updater')
cfg.update.max_backups = 10
logging.basicConfig(filename=str(backup/'apply.log'), level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s')
services = ['zenplus-api', 'zenplus-poller', 'zenplus-netflow-collector']
def active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit]).returncode == 0
timer_active = active('zenplus-updater.timer')
assert not active('zenplus-updater.service'), 'An updater is already active'
before = {'version':'1.23.11', 'services':{s:active(s) for s in services}, 'timer_active':timer_active}
(backup/'before.json').write_text(json.dumps(before,indent=2))
subprocess.run(['systemctl','stop','zenplus-updater.timer'],check=True)
try:
    with UpdateLock():
        assert not active('zenplus-updater.service')
        # The normal updater backs up code and PostgreSQL after stopping services.
        # Preserve the Python environment and local configuration as extra recovery material.
        venv = (installed/'venv').resolve()
        assert venv.parent == installed and venv.name.startswith('venv')
        with tarfile.open(backup/'python-environment.tar.gz','w:gz') as tar:
            tar.add(venv,arcname=venv.name)
        (backup/'python-environment-path.txt').write_text(str(venv)+'\n')
        with tarfile.open(backup/'local-configuration.tar.gz','w:gz') as tar:
            for name in ['/opt/zenplus/.env','/opt/zenplus/updater/config',
                         '/etc/zenplus','/etc/nginx','/etc/systemd/system','/etc/sudoers.d','/usr/local/sbin']:
                path=Path(name)
                if path.exists(): tar.add(path,arcname=str(path).lstrip('/'))
        subprocess.run(['gzip','-t',str(backup/'python-environment.tar.gz')],check=True)
        add_record(version='1.23.12',from_version='1.23.11',status='applying',changelog='Approved local development canary; unpublished signed candidate.')
        executed = False
        try:
            # Keep backups private, but do not pass that umask to dependency
            # installers or new application directories (the old updater is in memory).
            private_mask = os.umask(0o022)
            try:
                execute_manifest(manifest,str(extract),cfg)
            finally:
                os.umask(private_mask)
            executed = True
            schema = sync_and_verify()
            (backup/'schema.json').write_text(json.dumps(schema,indent=2))
            if not schema.get('ok'): raise RuntimeError('Post-update schema gate failed')
            for service in services:
                if not active(service): raise RuntimeError('Required service inactive: '+service)
            subprocess.run(['runuser','-u','zenplus','--','/opt/zenplus/venv/bin/python','-c',
                'from pysmi.parser.smi import parserFactory; import bcrypt,jinja2,lark; from passlib.context import CryptContext; c=CryptContext(schemes=["bcrypt_sha256"]); h=c.hash("dev-validation-only"); assert c.verify("dev-validation-only",h); assert parserFactory()'],
                check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            (installed/'.version').write_text('1.23.12\n'+datetime.now(timezone.utc).isoformat()+'\n')
            add_record(version='1.23.12',from_version='1.23.11',status='success',changelog='Approved local development canary; unpublished signed candidate.')
            result={'ok':True,'from_version':'1.23.11','version':'1.23.12','package_sha256':expected,'backup':str(backup),'schema_ok':True}
        except Exception as exc:
            if executed: rollback_manifest(manifest,str(extract),cfg)
            # Restore dependencies before restarting the old API, even if the
            # manifest executor already attempted its normal code/DB rollback.
            subprocess.run(['systemctl','stop',*services],check=False)
            with tarfile.open(backup/'python-environment.tar.gz','r:gz') as tar:
                tar.extractall(installed,filter='fully_trusted')
            subprocess.run(['systemctl','start',*services],check=False)
            add_record(version='1.23.12',from_version='1.23.11',status='failed',error=str(exc),changelog='Local dev canary failed; inspect recovery evidence.')
            result={'ok':False,'error':str(exc),'backup':str(backup)}
            (backup/'result.json').write_text(json.dumps(result,indent=2))
            raise
        (backup/'result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
finally:
    if timer_active: subprocess.run(['systemctl','start','zenplus-updater.timer'],check=True)
