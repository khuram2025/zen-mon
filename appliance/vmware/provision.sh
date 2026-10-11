#!/bin/bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
test "$(hostname)" = zentryc-golden
mkdir -p /var/log/zentryc
exec > /var/log/zentryc/image-build.log 2>&1
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl jq openssl python3 sudo \
  postgresql postgresql-client redis-server nginx docker.io docker-compose-v2 \
  snmp iputils-ping dnsutils iproute2 libcap2-bin cloud-image-utils lvm2 \
  openssh-server open-vm-tools nftables logrotate unattended-upgrades auditd apparmor ssl-cert \
  libexpat1 libsqlite3-0 libbz2-1.0 libffi8 liblzma5 zlib1g libgomp1
systemctl enable --now docker postgresql redis-server
test -b /dev/sdb
if ! pvs /dev/sdb >/dev/null 2>&1; then
  test "$(lsblk -no TYPE /dev/sdb)" = disk
  test -z "$(lsblk -no FSTYPE /dev/sdb)"
  pvcreate /dev/sdb
  vgcreate zentryc-data /dev/sdb
  lvcreate -l 85%FREE -n data zentryc-data
  mkfs.ext4 -L ZENTRYC_DATA /dev/zentryc-data/data
fi
mkdir -p /data
grep -q '/dev/zentryc-data/data' /etc/fstab || echo '/dev/zentryc-data/data /data ext4 defaults,nodev,nosuid 0 2' >> /etc/fstab
mount /data || mountpoint -q /data
id zenplus || useradd --system --home-dir /opt/zenplus --shell /usr/sbin/nologin zenplus
mkdir -p /opt/zenplus /root/zentryc-payload
tar -xzf /home/zimage/signed-runtime.tar.gz -C /root/zentryc-payload
cp -a /root/zentryc-payload/code/. /opt/zenplus/
install -m 0644 /home/zimage/init-postgres.sql /home/zimage/init-clickhouse.sql /opt/zenplus/scripts/
tar -xzf /home/zimage/python-runtime.tar.gz -C /
mkdir -p /opt/zenplus/bin /opt/zenplus/dashboard/dist /opt/zenplus/data/sensor-artifacts /opt/zenplus/updater/keys
cp /root/zentryc-payload/go-binaries/* /opt/zenplus/bin/
tar -xzf /root/zentryc-payload/dashboard-dist.tar.gz -C /opt/zenplus/dashboard/dist
cp -a /root/zentryc-payload/sensor-artifacts/. /opt/zenplus/data/sensor-artifacts/
cp /home/zimage/zentryc-release.pub /opt/zenplus/updater/keys/zentryc-release.pub
mkdir -p /data/{clickhouse,postgresql,redis,backups,tmp,zenplus-data,zenplus-logs} /var/lib/zentryc /etc/zentryc
systemctl stop postgresql redis-server
while read -r version cluster rest; do pg_ctlcluster "$version" "$cluster" stop || true; done < <(pg_lsclusters --no-header)
if ! test -L /var/lib/postgresql; then
 cp -a /var/lib/postgresql/. /data/postgresql/
 mv /var/lib/postgresql /var/lib/postgresql.base
 ln -s /data/postgresql /var/lib/postgresql
fi
if ! test -L /var/lib/redis; then
 cp -a /var/lib/redis/. /data/redis/
 mv /var/lib/redis /var/lib/redis.base
 ln -s /data/redis /var/lib/redis
fi
chown -R postgres:postgres /data/postgresql
chown -R redis:redis /data/redis
chown -R zenplus:zenplus /data/{backups,tmp,zenplus-data,zenplus-logs}
chown -R root:root /opt/zenplus
chown -R zenplus:zenplus /opt/zenplus/data /opt/zenplus/logs 2>/dev/null || true
for d in backups logs; do
  mkdir -p /opt/zenplus/$d
  chown -R zenplus:zenplus /opt/zenplus/$d
done
chmod -R a+rX /opt/zenplus/server /opt/zenplus/venv /opt/zentryc/python /opt/zenplus/dashboard/dist
mkdir -p /usr/local/libexec
install -m 0755 /home/zimage/first-boot.py /usr/local/libexec/zentryc-first-boot
install -m 0755 /home/zimage/clock.py /usr/local/libexec/zentryc-clock
install -m 0644 /home/zimage/zentryc-clock.service /etc/systemd/system/
for u in postgresql postgresql@18-main docker redis-server zentryc-first-boot nginx ssh; do
mkdir -p "/etc/systemd/system/$u.service.d"
cat > "/etc/systemd/system/$u.service.d/zentryc-clock.conf" <<'EOF'
[Unit]
Requires=zentryc-clock.service
After=zentryc-clock.service
EOF
done
systemctl enable zentryc-clock.service

# Reuse the release's service and helper recipes, without source fetch/build.
sed '$d' /home/zimage/install.sh > /root/zentryc-install-functions.sh
source /root/zentryc-install-functions.sh
set -Eeuo pipefail
systemctl() { case "$1" in restart|reload) return 0;; *) command systemctl "$@";; esac; }
create_services
setup_updater
setup_support_bundles
create_cli
unset -f systemctl
for hook in setup-security.sh setup-netflow-sysctl.sh install-clickhouse-config.sh; do bash /opt/zenplus/scripts/$hook; done
cp /opt/zenplus/scripts/systemd/zenplus-*.service /etc/systemd/system/
cp /opt/zenplus/scripts/systemd/zenplus-*.timer /etc/systemd/system/
cp /opt/zenplus/bin/wait-for-deps.sh /usr/local/libexec/zentryc-wait-for-deps
cat > /etc/systemd/system/zentryc-first-boot.service <<'EOF'
[Unit]
Description=Zentryc appliance identity and offline initialization
After=postgresql.service docker.service redis-server.service
Requires=postgresql.service docker.service
Before=zenplus-wait-deps.service nginx.service ssh.service
ConditionPathExists=!/var/lib/zentryc/initialized.json
[Service]
Type=oneshot
ExecStart=/usr/local/libexec/zentryc-first-boot
TimeoutStartSec=1800
RemainAfterExit=yes
UMask=0077
[Install]
WantedBy=multi-user.target
EOF
mkdir -p /etc/systemd/system/{zenplus-wait-deps,nginx,ssh}.service.d
for u in zenplus-wait-deps nginx ssh; do
cat > /etc/systemd/system/$u.service.d/zentryc-first-boot.conf <<'EOF'
[Unit]
Requires=zentryc-first-boot.service
After=zentryc-first-boot.service
EOF
done
cat > /etc/nginx/conf.d/zenplus.conf <<'EOF'
server { listen 80; listen [::]:80; server_name _; return 301 https://$host$request_uri; }
server {
 listen 443 ssl; listen [::]:443 ssl; http2 on;
 ssl_certificate /etc/zenplus/tls/server.crt;
 ssl_certificate_key /etc/zenplus/tls/server.key;
 ssl_protocols TLSv1.2 TLSv1.3;
 server_tokens off;
 root /opt/zenplus/dashboard/dist; index index.html;
 add_header X-Content-Type-Options nosniff always;
 add_header X-Frame-Options SAMEORIGIN always;
 location / { try_files $uri $uri/ /index.html; }
 location /api/ {
  proxy_pass http://127.0.0.1:8000;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1; proxy_buffering off; proxy_read_timeout 3600s;
  client_max_body_size 100m;
 }
 location /v1/ {
  proxy_pass http://127.0.0.1:8000;
  proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_buffering off; client_max_body_size 32m; proxy_read_timeout 120s;
 }
}
EOF
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
table inet zentryc {
 chain input {
  type filter hook input priority 0; policy drop;
  iif lo accept
  ct state established,related accept
  ct state invalid drop
  ip protocol icmp accept
  meta l4proto ipv6-icmp accept
  udp sport 67 udp dport 68 accept
  udp sport 547 udp dport 546 accept
  tcp dport {22,80,443} accept
  udp dport {162,2055,6343} accept
 }
 chain forward { type filter hook forward priority -10; policy accept; }
 chain output { type filter hook output priority 0; policy accept; }
}
EOF
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/zentryc.conf <<'EOF'
[Journal]
SystemMaxUse=256M
RuntimeMaxUse=64M
MaxRetentionSec=7day
Compress=yes
EOF
cat > /etc/sysctl.d/90-zentryc.conf <<'EOF'
fs.suid_dumpable=0
kernel.kptr_restrict=2
kernel.dmesg_restrict=1
kernel.randomize_va_space=2
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.all.accept_source_route=0
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.default.accept_redirects=0
net.ipv4.ping_group_range=0 2147483647
EOF
cat > /etc/ssh/sshd_config.d/10-zentryc.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
MaxAuthTries 3
LoginGraceTime 30
Banner /etc/issue.net
EOF
printf '\nZentryc ZenPlus Appliance 1.24.14\nAuthorized access only. https://zentryc.com/\n' > /etc/issue.net
cp /etc/issue.net /etc/issue
printf 'Zentryc ZenPlus Appliance\nUse the local setup console or HTTPS management interface.\n' > /etc/motd
cat >> /etc/os-release <<'EOF'
ZENTRYC_PRODUCT=ZenPlus
ZENTRYC_APPLIANCE_VERSION=1.24.14
ZENTRYC_SUPPORT_URL=https://zentryc.com/support/
EOF
mkdir -p /etc/default/grub.d
printf 'GRUB_DISTRIBUTOR="Zentryc ZenPlus"\nGRUB_TIMEOUT=3\n' > /etc/default/grub.d/zentryc.cfg
update-grub
systemctl disable --now motd-news.timer apport.service snapd.service snapd.socket 2>/dev/null || true
systemctl enable zentryc-first-boot.service zenplus-api.service zenplus-poller.service zenplus-netflow-collector.service nginx.service zenplus-updater.timer zenplus-storage-watchdog.service zenplus-ncm-worker.service zenplus-ncm-backup.timer zenplus-ncm-delivery.timer nftables.service
systemctl daemon-reload
sysctl --system
# Pin the packaged ClickHouse image by digest after pulling on the build network.
docker pull clickhouse/clickhouse-server:24-alpine
digest=$(docker image inspect --format '{{index .RepoDigests 0}}' clickhouse/clickhouse-server:24-alpine)
sed -i "s|image: clickhouse/clickhouse-server:24-alpine|image: $digest|" /opt/zenplus/docker-compose.yml
printf '%s\n' "$digest" > /etc/zentryc/clickhouse-image.txt
systemctl start postgresql redis-server
while read -r version cluster rest; do pg_ctlcluster "$version" "$cluster" start || true; done < <(pg_lsclusters --no-header)
systemctl start zentryc-first-boot.service
systemctl restart zenplus-wait-deps zenplus-api zenplus-poller zenplus-netflow-collector nginx zenplus-ncm-worker zenplus-storage-watchdog
/usr/local/sbin/zenplus-security-helper apply https=on redirect=on hsts=off min_tls=1.2
systemctl restart nftables
echo 'PROVISION COMPLETE'
