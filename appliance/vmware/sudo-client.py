#!/usr/bin/python3
"""Route the service account's existing typed sudo calls through one boundary."""
import os,pwd,sys,json,subprocess
if pwd.getpwuid(os.getuid()).pw_name!='zenplus': os.execv('/usr/bin/sudo',['sudo',*sys.argv[1:]])
args=sys.argv[1:]
if args[:1]==['-n']: args=args[1:]
if not args or args[0].startswith('-'): raise SystemExit('Unsupported privilege request')
payload={'argv':args,'stdin':sys.stdin.read(2048) if args[0]=='/usr/bin/tee' else ''}
p=subprocess.run(['/usr/bin/sudo','-n','/usr/local/libexec/zentryc-privileged'],input=json.dumps(payload).encode())
raise SystemExit(p.returncode)
