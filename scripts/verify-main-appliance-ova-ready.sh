#!/usr/bin/env bash
# Verify the full ZenPlus appliance VM is clean and ready for OVA export.
set -euo pipefail

ZENPLUS_HOME="${ZENPLUS_HOME:-/opt/zenplus}"
SENTINEL="${ZENPLUS_SENTINEL:-/var/lib/zenplus/.initialized}"
errs=0

ok() { printf 'OK   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; errs=$((errs + 1)); }
check() {
  local cmd="$1"
  local label="$2"
  if bash -c "$cmd" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi
}

echo "=== ZenPlus full appliance OVA readiness ==="
echo

echo "[required files]"
check "test -d '$ZENPLUS_HOME'" "/opt/zenplus exists"
check "test -f '$ZENPLUS_HOME/dashboard/dist/index.html'" "dashboard dist exists"
check "test -x '$ZENPLUS_HOME/bin/zenplus-poller'" "poller binary exists"
check "test -f '$ZENPLUS_HOME/updater/keys/zentryc-release.pub'" "release public key exists"
check "! find '$ZENPLUS_HOME/updater/keys' -type f \\( -name '*.key' -o -name '*.pem' \\) | grep -q ." "no private signing key in image"

echo
echo "[first boot state]"
check "! test -f '$ZENPLUS_HOME/.env'" ".env absent"
check "! test -f '$SENTINEL'" "first-boot sentinel absent"
check "! test -f /var/lib/zenplus/initial-admin-password" "initial admin password absent before export"
check "! test -f '$ZENPLUS_HOME/updater/config/subscription.json'" "subscription cache absent"
check "! grep -Eq '^id[[:space:]]*=[[:space:]]*[^[:space:]]+' '$ZENPLUS_HOME/updater/config/agent.conf'" "appliance id blank"
check "! grep -Eq '^api_key[[:space:]]*=[[:space:]]*[^[:space:]]+' '$ZENPLUS_HOME/updater/config/agent.conf'" "appliance api key blank"

echo
echo "[services]"
check "systemctl is-enabled zenplus-first-boot.service >/dev/null" "zenplus-first-boot enabled"
check "systemctl is-enabled zenplus-api.service >/dev/null" "zenplus-api enabled"
check "systemctl is-enabled zenplus-poller.service >/dev/null" "zenplus-poller enabled"
check "systemctl is-enabled nginx.service >/dev/null" "nginx enabled"
check "systemctl is-enabled zenplus-updater.timer >/dev/null" "updater timer enabled"
check "! systemctl is-active --quiet zenplus-api.service" "zenplus-api stopped"
check "! systemctl is-active --quiet zenplus-poller.service" "zenplus-poller stopped"

echo
echo "[logs and identities]"
check "! find /tmp /var/tmp -mindepth 1 -maxdepth 1 2>/dev/null | grep -q ." "temporary directories empty"
check "! find /root /home -maxdepth 2 -type f \\( -name '.bash_history' -o -name '.zsh_history' -o -name '.python_history' \\) -size +0c 2>/dev/null | grep -q ." "shell histories empty"
check "test ! -s /etc/machine-id" "machine-id empty for regeneration"

echo
if [[ "$errs" -eq 0 ]]; then
  echo "=== READY FOR OVA EXPORT ==="
  exit 0
fi

echo "=== NOT READY: $errs failure(s) ==="
exit 1
