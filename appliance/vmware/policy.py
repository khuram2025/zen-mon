#!/usr/bin/python3
import os,subprocess,pwd,stat
from pathlib import Path
if os.geteuid()!=0: raise SystemExit('Root required')
os.chmod('/etc/sudoers.d',0o755)  # API may inspect the helper marker; grants stay 0440.
policy='zenplus ALL=(root) NOPASSWD: /usr/local/libexec/zentryc-privileged ""\n'
# The API checks this existing marker path before invoking the security helper.
# Its contents grant only the new argument-free broker, never the old helper.
p=Path('/etc/sudoers.d/zenplus-security');tmp=p.with_name(p.name+'.tmp');tmp.write_text(policy);tmp.chmod(0o440)
subprocess.run(['/usr/sbin/visudo','-cf',str(tmp)],check=True)
os.replace(tmp,p)
for name in ['zenplus-updater','zentryc-privileged','zenplus-access','zenplus-storage']:
    (Path('/etc/sudoers.d')/name).unlink(missing_ok=True)
subprocess.run(['/usr/sbin/visudo','-c'],check=True)
lock=Path('/opt/zenplus/support/.enqueue.lock')
fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o640)
if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit('Unsafe support lock')
account=pwd.getpwnam('zenplus');os.fchown(fd,account.pw_uid,account.pw_gid);os.fchmod(fd,0o640);os.close(fd)
print('Zentryc typed privilege policy applied')
