#!/bin/bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
test -f /etc/zentryc/identity.json
systemctl stop zenplus-api zenplus-poller zenplus-netflow-collector zenplus-ncm-worker
apt-mark manual netplan.io systemd systemd-sysv chrony "linux-image-$(uname -r)" >/dev/null
remove=()
for pkg in cloud-init cloud-init-base git git-man snapd apport apport-core-dump-handler \
 lxd-installer multipath-tools open-iscsi fwupd modemmanager packagekit \
 ubuntu-advantage-tools ubuntu-pro-client landscape-common pollinate \
 ubuntu-release-upgrader-core update-manager-core command-not-found \
 linux-headers-generic linux-headers-virtual; do
 if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then remove+=("$pkg"); fi
done
if ((${#remove[@]})); then apt-get purge -y "${remove[@]}"; fi
apt-get autoremove --purge -y
apt-get clean
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${Status}\n' > /etc/zentryc/packages.tsv
systemctl daemon-reload
systemctl restart zenplus-wait-deps zenplus-api zenplus-poller zenplus-netflow-collector zenplus-ncm-worker
echo MINIMIZATION_COMPLETE
