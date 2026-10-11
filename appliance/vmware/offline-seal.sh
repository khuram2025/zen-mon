#!/bin/bash
# Finish sealing a powered-off lab image. Run only on the isolated builder.
set -Eeuo pipefail
test "$EUID" = 0
root=/mnt/zentryc-offline-root
data=/mnt/zentryc-offline-data
test "$(findmnt -no SOURCE "$root")" = /dev/nbd0p1
test "$(findmnt -no SOURCE "$data")" = /dev/mapper/zentryc--data-data
test "$(head -n 1 "$root/opt/zenplus/.version")" = 1.24.14
case "$(cat "$root/etc/hostname")" in zentryc-golden|zentryc-appliance) ;; *) exit 1;; esac
test ! -e "$root/opt/zenplus/.env"
test ! -e "$root/etc/zentryc/identity.json"
test ! -e "$root/var/lib/zentryc/initial-login.json"
python3 - <<'PY'
from pathlib import Path
root=Path('/mnt/zentryc-offline-root')
for name in ('passwd','shadow','group','gshadow'):
 p=root/'etc'/name
 lines=p.read_text().splitlines()
 updated=[]
 for line in lines:
  fields=line.split(':')
  if fields[0]=='zimage':continue
  if name in ('group','gshadow'):
   fields[-1]=','.join(x for x in fields[-1].split(',') if x!='zimage')
  updated.append(':'.join(fields))
 p.write_text('\n'.join(updated)+'\n')
(root/'etc/hostname').write_text('zentryc-appliance\n')
p=root/'etc/hosts';p.write_text(p.read_text().replace('zentryc-golden','zentryc-appliance'))
PY
find "$root/home" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
for name in zentryc-payload zentryc-final-payload .cache .ssh; do rm -rf -- "$root/root/$name"; done
rm -rf -- "$root/var/lib/cloud" "$root/var/lib/postgresql.base" "$root/var/lib/redis.base" "$root/etc/zentryc/package-notices"
rm -f -- "$root/root/zentryc-install-functions.sh" "$root/root/.bash_history" "$root/root/.python_history"
rm -f -- "$root/etc/sudoers.d/90-zimage" "$root/etc/sudoers.d/90-cloud-init-users" "$root/etc/sudoers.d/zimage" "$root/etc/sudoers.d/zenplus-security.tmp"
rm -f -- "$root/etc/passwd-" "$root/etc/shadow-" "$root/etc/group-" "$root/etc/gshadow-"
find "$root/tmp" "$root/var/tmp" -mindepth 1 -delete
find "$root/etc/netplan" -maxdepth 1 -name '*.yaml' -delete
cat > "$root/etc/netplan/50-zentryc.yaml" <<'EOF'
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
chmod 600 "$root/etc/netplan/50-zentryc.yaml"
truncate -s 0 "$root/etc/machine-id"
rm -f "$root/var/lib/dbus/machine-id" "$root/var/lib/systemd/random-seed" "$root/var/lib/docker/engine-id"
ln -s /etc/machine-id "$root/var/lib/dbus/machine-id"
find "$root/var/lib/dhcp" -type f -delete 2>/dev/null || true
find "$root/opt/zenplus" "$root/opt/zentryc/python" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$root/var/log" -type f ! -path '*/journal/*' -exec truncate -c -s 0 {} +
find "$root/var/log/journal" -mindepth 1 -delete
test -z "$(find "$root/home" -mindepth 1 -print -quit)"
test -z "$(find "$root/opt/zenplus" -type f -name '*.key' -print -quit)"
chown -R root:root "$root/opt/zenplus/data/sensor-artifacts"
chmod -R a+rX "$root/opt/zenplus/data/sensor-artifacts"
systemctl --root="$root" enable zentryc-first-boot zenplus-api zenplus-poller zenplus-netflow-collector zenplus-ncm-worker zenplus-ncm-backup.timer zenplus-ncm-delivery.timer zenplus-support-queue.path zenplus-updater.timer nginx zentryc-console
sync
# The mount-source assertions above keep these writes on the copied image disks.
for mount in "$root" "$data"; do
 dd if=/dev/zero of="$mount/zentryc-zero-free" bs=16M status=none || test "$?" = 1
 rm -f "$mount/zentryc-zero-free"
done
sync
echo OFFLINE_SEAL_COMPLETED
