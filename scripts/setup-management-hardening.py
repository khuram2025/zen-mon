#!/usr/bin/python3
"""Persist management hardening across appliance installation and updates.
Run as root. Existing management allowlists and passwords are preserved.
"""
import os
from pathlib import Path
import subprocess
if os.geteuid() != 0:
    raise SystemExit('Root required')
def write(path, text, mode=0o644):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text);os.chown(path,0,0);path.chmod(mode)
write('/etc/systemd/system/zenplus-api.service.d/security.conf', '[Service]\nExecStart=\nExecStart=/opt/zenplus/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2 --no-access-log\n')
write('/etc/systemd/system/zenplus-netflow-collector.service.d/security.conf', '[Service]\nEnvironment=NETFLOW_HEALTH_LISTEN=127.0.0.1:8091\n')
known=Path('/etc/zenplus/known_hosts')
if not known.exists():write(known, '# Add device SSH host keys after independently verifying fingerprints.\n')
write('/etc/ssh/sshd_config.d/00-zenplus-security.conf', 'PermitRootLogin no\nX11Forwarding no\nAllowTcpForwarding no\nAllowAgentForwarding no\nPermitTunnel no\nMaxAuthTries 3\nLoginGraceTime 30\nMaxStartups 10:30:30\n')
subprocess.run(['/usr/sbin/sshd','-t'],check=True)
p=Path('/etc/nginx/nginx.conf');config=p.read_text()
if 'log_format zenplus_safe ' not in config:
    config=config.replace('http {', "http {\n    log_format zenplus_safe '$remote_addr - $remote_user [$time_local] \"$request_method $uri $server_protocol\" $status $body_bytes_sent';",1)
config=config.replace('access_log /var/log/nginx/access.log;', 'access_log /var/log/nginx/access.log zenplus_safe;')
write(p, config)
subprocess.run(['nginx','-t'],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
# Service restarts stay under installer/updater control to avoid surprise downtime.
