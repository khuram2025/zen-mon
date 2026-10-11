#!/usr/bin/python3
"""Fixed timer operation; no caller-supplied systemd directives or paths."""
import os,sys,re,subprocess
from pathlib import Path
if os.geteuid()!=0 or len(sys.argv)!=1: raise SystemExit('Root and no arguments required')
raw=sys.stdin.buffer.read(32).decode('ascii',errors='strict').strip()
if not re.fullmatch(r'[0-9]{1,3}',raw) or not 1<=int(raw)<=168:
    raise SystemExit('Interval must be 1 through 168 hours')
p=Path('/etc/systemd/system/zenplus-updater.timer.d')
if p.is_symlink(): raise SystemExit('Invalid timer directory')
p.mkdir(mode=0o755,parents=True,exist_ok=True)
target=p/'override.conf'
if target.is_symlink(): raise SystemExit('Invalid timer file')
temp=p/'zentryc.tmp'
fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
with os.fdopen(fd,'w') as out: out.write(f'[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec={int(raw)}h\n')
os.replace(temp,target)
subprocess.run(['/bin/systemctl','daemon-reload'],check=True)
subprocess.run(['/bin/systemctl','restart','zenplus-updater.timer'],check=True)
