#!/usr/bin/env bash
# Prepare the full ZenPlus appliance VM for OVA export.
#
# This script is intentionally destructive. Run it only on the golden image VM
# immediately before powering off and exporting from the hypervisor.
set -euo pipefail

YES=0
KEEP_SSH_HOST_KEYS=0

usage() {
  cat <<'EOF'
usage: sudo scripts/prepare-main-appliance-ova.sh --yes [--keep-ssh-host-keys]

Prepares the current VM as a ZenPlus golden appliance image:
  - stops ZenPlus services
  - removes generated appliance secrets
  - removes OTA registration state
  - clears logs, histories, caches, and machine identity
  - leaves first-boot initialization ready to run on next boot

Run this only on the golden image VM immediately before OVA export.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --keep-ssh-host-keys) KEEP_SSH_HOST_KEYS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root" >&2
  exit 1
fi

if [[ "$YES" != "1" ]]; then
  echo "refusing to run without --yes" >&2
  usage >&2
  exit 2
fi

ZENPLUS_HOME="${ZENPLUS_HOME:-/opt/zenplus}"
ZENPLUS_USER="${ZENPLUS_USER:-zenplus}"
SENTINEL="${ZENPLUS_SENTINEL:-/var/lib/zenplus/.initialized}"

log() { printf '[prep-ova] %s\n' "$*"; }

require_path() {
  if [[ ! -e "$1" ]]; then
    echo "required path missing: $1" >&2
    exit 1
  fi
}

require_path "$ZENPLUS_HOME"

log "stopping application services"
systemctl stop zenplus-updater.timer 2>/dev/null || true
systemctl stop zenplus-updater.service 2>/dev/null || true
systemctl stop zenplus-poller.service 2>/dev/null || true
systemctl stop zenplus-api.service 2>/dev/null || true
systemctl stop zenplus-wait-deps.service 2>/dev/null || true
systemctl stop nginx.service 2>/dev/null || true

log "stopping clickhouse container if present"
(cd "$ZENPLUS_HOME" && docker compose stop clickhouse >/dev/null 2>&1) || true
(cd "$ZENPLUS_HOME" && docker compose down >/dev/null 2>&1) || true

log "removing generated appliance state"
rm -f "$ZENPLUS_HOME/.env"
rm -f "$SENTINEL"
rm -f /var/lib/zenplus/.initialized
rm -f /var/lib/zenplus/initial-admin-password
rm -f "$ZENPLUS_HOME/updater/config/subscription.json"
rm -f "$ZENPLUS_HOME/updater/logs/update.log"*
if [[ -d /data/clickhouse ]]; then
  find /data/clickhouse -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi

if [[ -f "$ZENPLUS_HOME/updater/config/agent.conf" ]]; then
  sed -i -E 's|^(id[[:space:]]*=).*|\1|; s|^(api_key[[:space:]]*=).*|\1|' \
    "$ZENPLUS_HOME/updater/config/agent.conf"
  chown "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME/updater/config/agent.conf" 2>/dev/null || true
  chmod 0600 "$ZENPLUS_HOME/updater/config/agent.conf"
fi

log "removing generated release/build secrets"
find "$ZENPLUS_HOME" -path '*/updater/keys/*' -type f \
  \( -name '*.key' -o -name '*.pem' \) -delete
find "$ZENPLUS_HOME" -type f \( -name '.env' -o -name '*.secret' \) -delete

log "clearing logs"
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
find "$ZENPLUS_HOME" -path '*/logs/*' -type f -exec truncate -s 0 {} \; 2>/dev/null || true

log "clearing shell histories and temporary files"
find /root /home -maxdepth 2 -type f \
  \( -name '.bash_history' -o -name '.zsh_history' -o -name '.python_history' \) \
  -exec truncate -s 0 {} \; 2>/dev/null || true
rm -rf /tmp/* /var/tmp/* 2>/dev/null || true
rm -rf /root/.cache /home/*/.cache 2>/dev/null || true
rm -rf /root/.npm /home/*/.npm /root/go/pkg/mod/cache 2>/dev/null || true

log "clearing cloud-init and machine identity"
cloud-init clean --logs --seed 2>/dev/null || true
truncate -s 0 /etc/machine-id 2>/dev/null || true
rm -f /var/lib/dbus/machine-id 2>/dev/null || true
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

if [[ "$KEEP_SSH_HOST_KEYS" != "1" ]]; then
  log "removing ssh host keys so imported appliances generate unique keys"
  rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
fi

log "normalizing ownership and permissions"
chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME" 2>/dev/null || true
chmod 0750 "$ZENPLUS_HOME" 2>/dev/null || true
chmod 0644 "$ZENPLUS_HOME/updater/keys/zentryc-release.pub" 2>/dev/null || true
chmod 0600 "$ZENPLUS_HOME/updater/config/agent.conf" 2>/dev/null || true

log "enabling first-boot and runtime services for next import"
systemctl enable zenplus-first-boot.service 2>/dev/null || true
systemctl enable zenplus-wait-deps.service zenplus-api.service zenplus-poller.service nginx.service 2>/dev/null || true
systemctl enable zenplus-updater.timer 2>/dev/null || true
systemctl daemon-reload

log "running export readiness verification"
VERIFY_SCRIPT="$(dirname "$0")/verify-main-appliance-ova-ready.sh"
if [[ ! -x "$VERIFY_SCRIPT" ]] && command -v zenplus-verify-main-appliance-ova-ready >/dev/null 2>&1; then
  VERIFY_SCRIPT="$(command -v zenplus-verify-main-appliance-ova-ready)"
fi
"$VERIFY_SCRIPT"

cat <<'EOF'

Golden image is prepared.

Next steps:
  1. Shut this VM down now.
  2. Export it from the VMware/ESXi/Workstation host as OVA.
  3. Generate and publish a SHA-256 checksum for the OVA.

Do not boot this prepared golden image again before export unless you intend
to rerun first boot and then prepare it again.
EOF
