#!/usr/bin/python3
import os,subprocess
from pathlib import Path
if os.geteuid()!=0: raise SystemExit('Root required')
policy='zenplus ALL=(root) NOPASSWD: /usr/local/libexec/zentryc-privileged ""\n'
p=Path('/etc/sudoers.d/zentryc-privileged');tmp=p.with_name(p.name+'.tmp');tmp.write_text(policy);tmp.chmod(0o440)
subprocess.run(['/usr/sbin/visudo','-cf',str(tmp)],check=True)
os.replace(tmp,p)
for name in ['zenplus-updater','zenplus-security','zenplus-access','zenplus-storage']:
    (Path('/etc/sudoers.d')/name).unlink(missing_ok=True)
subprocess.run(['/usr/sbin/visudo','-c'],check=True)
print('Zentryc typed privilege policy applied')