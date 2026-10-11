#!/bin/bash
# Destructive: run only on a disposable, validated golden VM before export.
set -Eeuo pipefail
test "$EUID" = 0
test "$(hostname)" = zentryc-golden
test -f /etc/zentryc/identity.json
test -f /etc/zentryc/release/manifest.json
test "$(findmnt -no SOURCE /data)" = /dev/mapper/zentryc--data-data
systemctl stop zenplus-updater.timer zenplus-ncm-backup.timer zenplus-ncm-delivery.timer zenplus-support-queue.path zenplus-support-cleanup.timer
systemctl stop zenplus-api zenplus-poller zenplus-netflow-collector zenplus-ncm-worker zenplus-storage-watchdog nginx zentryc-console
cd /opt/zenplus
docker compose down
systemctl stop docker.service docker.socket redis-server postgresql
pg_dropcluster --stop 18 main
pg_createcluster 18 main --start-conf auto -- --auth-local peer --auth-host scram-sha-256
sed -i '/^requirepass /d;/^user default /d' /etc/redis/redis.conf
# All customer data and disposable lab state are removed while services are stopped.
for d in /data/clickhouse /data/redis /data/backups /data/tmp /data/zenplus-data /data/zenplus-logs /opt/zenplus/backups /opt/zenplus/logs; do
 test -d "$d"; find "$d" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
done
for d in /opt/zenplus/support/requests /opt/zenplus/support/jobs /opt/zenplus/support/bundles; do
 test ! -d "$d" || find "$d" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
done
find /opt/zenplus/data -mindepth 1 -maxdepth 1 ! -name sensor-artifacts -exec rm -rf -- {} +
find /opt/zenplus/updater -maxdepth 1 -type d \( -name state -o -name backups -o -name downloads -o -name logs \) -exec sh -c 'find "$1" -mindepth 1 -delete' sh {} \;
rm -f /opt/zenplus/.env /var/lib/zenplus/.initialized /var/lib/zenplus/initial-admin-password /etc/zentryc/identity.json
find /var/lib/zentryc -mindepth 1 -delete
chmod 700 /var/lib/zentryc
rm -f /etc/zenplus/tls/server.key /etc/zenplus/tls/server.crt /etc/zenplus/tls/fullchain.pem /etc/ssh/ssh_host_*
rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem
printf '{}\n' > /opt/zenplus/updater/config/subscription.json
python3 - <<'PY'
from pathlib import Path
import configparser
p=Path('/opt/zenplus/updater/config/agent.conf')
c=configparser.ConfigParser();c.read(p)
for section in c.sections():
 for name in ('id','api_key','appliance_id','token','license_key'):
  if c.has_option(section,name):c.set(section,name,'')
if c.has_section('update'):c.set('update','auto_update','True')
if c.has_section('server'):c.set('server','check_interval_seconds','14400')
with p.open('w') as f:c.write(f)
PY
chown zenplus:zenplus /opt/zenplus/updater/config/*
chmod 600 /opt/zenplus/updater/config/*
rm -f /etc/systemd/system/zenplus-updater.timer.d/override.conf
rm -f /etc/sudoers.d/90-zimage /etc/sudoers.d/90-cloud-init-users /etc/sudoers.d/zimage /etc/sudoers.d/zenplus-security.tmp
userdel zimage
find /home -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
rm -rf /root/zentryc-payload /root/.cache /root/.ssh /var/lib/cloud /var/lib/postgresql.base /var/lib/redis.base /etc/zentryc/package-notices
rm -f /root/zentryc-install-functions.sh /root/.bash_history /root/.python_history
find /tmp /var/tmp -mindepth 1 -delete
find /etc/netplan -maxdepth 1 -name '*.yaml' -delete
cat > /etc/netplan/50-zentryc.yaml <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    management:
      match:
        name: "en*"
      dhcp4: true
      optional: true
EOF
chmod 600 /etc/netplan/50-zentryc.yaml
hostnamectl set-hostname zentryc-appliance
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
ln -s /etc/machine-id /var/lib/dbus/machine-id
rm -f /var/lib/dhcp/* /run/systemd/netif/leases/*
apt-get clean
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${Status}\n' > /etc/zentryc/packages.tsv
find /opt/zenplus /opt/zentryc/python -type d -name __pycache__ -prune -exec rm -rf -- {} +
find /var/log -type f ! -path '/var/log/journal/*' -exec truncate -s 0 {} +
journalctl --rotate
journalctl --vacuum-time=1s
systemctl daemon-reload
systemctl enable zentryc-first-boot zenplus-api zenplus-poller zenplus-netflow-collector zenplus-ncm-worker zenplus-ncm-backup.timer zenplus-ncm-delivery.timer zenplus-support-queue.path zenplus-updater.timer nginx
/usr/local/libexec/zentryc-policy
test ! -f /opt/zenplus/.env
test ! -f /var/lib/zentryc/initialized.json
test -z "$(find /home -mindepth 1 -print -quit)"
test -z "$(find /opt/zenplus -type f -name '*.key' -print -quit)"
sync
# Zero free blocks so removed lab secrets cannot survive in exported disk sectors.
for mount in / /data; do
 dd if=/dev/zero of="$mount/zentryc-zero-free" bs=16M status=none || test "$?" = 1
 rm -f "$mount/zentryc-zero-free"
done
sync
systemctl poweroff
