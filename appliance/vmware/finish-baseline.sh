#!/bin/bash
set -Eeuo pipefail
mkdir -p /etc/systemd/system/zenplus-updater.service.d
mkdir -p /etc/systemd/system/zenplus-api.service.d /etc/systemd/system/zenplus-poller.service.d
printf '[Service]\nEnvironment=PYTHONPATH=/opt/zenplus\n' > /etc/systemd/system/zenplus-api.service.d/zentryc.conf
install -o zenplus -g zenplus -m 0640 /dev/null /opt/zenplus/support/.enqueue.lock
printf '[Service]\nAmbientCapabilities=CAP_NET_RAW CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE\nProtectSystem=strict\nProtectHome=true\nReadWritePaths=/opt/zenplus/logs /opt/zenplus/data /var/lib/zenplus\n' > /etc/systemd/system/zenplus-poller.service.d/zentryc.conf
printf '[Service]\nExecStopPost=/usr/local/libexec/zentryc-policy\n' > /etc/systemd/system/zenplus-updater.service.d/zentryc-policy.conf
for unit in zenplus-ncm-worker zenplus-ncm-backup zenplus-ncm-delivery; do
 mkdir -p /etc/systemd/system/$unit.service.d
 printf '[Service]\nReadWritePaths=/opt/zenplus/backups /opt/zenplus/data /opt/zenplus/logs /data/backups /var/lib/zenplus\n' > /etc/systemd/system/$unit.service.d/zentryc.conf
done
cat > /etc/systemd/system/zentryc-console.service <<'EOF'
[Unit]
Description=Zentryc appliance setup and recovery console
After=systemd-user-sessions.service zentryc-first-boot.service
Conflicts=getty@tty1.service
[Service]
Type=idle
ExecStart=/usr/local/libexec/zentryc-console
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Restart=always
RestartSec=2
UMask=0077
[Install]
WantedBy=multi-user.target
EOF
systemctl mask getty@tty1.service
systemctl enable zentryc-console.service
systemctl daemon-reload
systemctl restart zenplus-ncm-worker
systemctl start zentryc-console
# Preserve Ubuntu identity for security scanners and add truthful product fields.
printf 'Zentryc appliance console. HTTPS management; unique first-boot password.\n' > /etc/motd
rm -f /etc/update-motd.d/50-motd-news /etc/update-motd.d/91-contract-ua-esm-status /etc/update-motd.d/91-release-upgrade /etc/update-motd.d/98-reboot-required
mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any --timeout=15\n' > /etc/systemd/system/systemd-networkd-wait-online.service.d/zentryc.conf
apt-get -y upgrade
apt-get purge -y snapd apport apport-core-dump-handler whoopsie popularity-contest ubuntu-report 2>/dev/null || true
apt-get clean
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${Status}\n' > /etc/zentryc/packages.tsv
/opt/zenplus/venv/bin/pip freeze > /etc/zentryc/python-runtime.lock
printf 'Third-party copyright notices: /usr/share/doc/*/copyright\n' > /etc/zentryc/NOTICE
systemctl daemon-reload
echo BASELINE_COMPLETE
