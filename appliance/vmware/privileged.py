#!/usr/bin/python3
"""Bounded typed operations. No shell, executable selection or arbitrary writes."""
import os,sys,json,re,subprocess
from pathlib import Path
if os.geteuid()!=0 or len(sys.argv)!=1 or os.environ.get('SUDO_USER')!='zenplus': raise SystemExit('Unauthorized caller')
raw=sys.stdin.buffer.read(8193)
if len(raw)>8192: raise SystemExit('Request too large')
try: request=json.loads(raw)
except ValueError: raise SystemExit('Invalid request')
args=request.get('argv');data=request.get('stdin','')
if not isinstance(args,list) or not 1<=len(args)<=12 or any(not isinstance(a,str) or len(a)>512 or '\x00' in a or '\n' in a for a in args): raise SystemExit('Invalid arguments')
allowed={
 '/usr/local/sbin/zenplus-security-helper':{'install-cert','apply','remove-cert'},
 '/usr/local/sbin/zenplus-access-helper':{'apply'},
 '/usr/local/sbin/zenplus-storage-helper':{'rescan','pg-dump','pg-restore','pg-createdb','pg-dropdb','os-usage','vacuum-journal','apt-clean','restart-app-services'},
 '/usr/local/sbin/zenplus-volume-helper':{'inventory','rescan','add-disk','extend','evacuate','abort-evacuation','remove-disk','remove-missing'},
 '/usr/local/sbin/zenplus-lvm-helper':{'vgs','lvs','pvs','pvresize'},
}
cmd=args[0]
if cmd in allowed:
    if len(args)<2 or args[1] not in allowed[cmd] or data: raise SystemExit('Unsupported operation')
elif cmd=='/usr/local/sbin/zenplus-expand-storage':
    if len(args)>2 or (len(args)==2 and not re.fullmatch(r'/dev/[a-z0-9]+',args[1])): raise SystemExit('Invalid disk')
elif cmd=='/usr/local/sbin/zentryc-updater-timer':
    if len(args)!=1: raise SystemExit('Invalid timer request')
elif cmd in {'/bin/systemctl','/usr/bin/systemctl'}:
    if args[1:] not in [['--no-block','start','zenplus-updater.service'],['start','zenplus-updater.service'],['restart','zenplus-updater.timer'],['daemon-reload']]: raise SystemExit('Invalid service operation')
elif cmd=='/usr/bin/du':
    if len(args)!=3 or args[1]!='-sb' or args[2] not in ['/data/'+d for d in ['clickhouse','postgresql','redis','backups','tmp']]: raise SystemExit('Invalid usage request')
elif cmd=='/bin/mkdir' and args[1:]==['-p','/etc/systemd/system/zenplus-updater.timer.d']:
    Path('/etc/systemd/system/zenplus-updater.timer.d').mkdir(mode=0o755,parents=True,exist_ok=True);raise SystemExit(0)
elif cmd=='/usr/bin/tee' and args[1:]==['/etc/systemd/system/zenplus-updater.timer.d/override.conf']:
    m=re.fullmatch(r'# Managed by zenplus-api — do not edit by hand[.]\n\[Timer\]\nOnUnitActiveSec=\nOnUnitActiveSec=([0-9]{1,3})h\n',data)
    if not m: raise SystemExit('Unsupported timer directives')
    args=['/usr/local/sbin/zentryc-updater-timer'];data=m.group(1)
else: raise SystemExit('Unsupported privileged executable')
os.environ.clear();os.environ.update({'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
raise SystemExit(subprocess.run(args,input=data.encode() if data else None,timeout=300).returncode)
