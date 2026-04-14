#!/bin/bash
# Pre-snapshot verification. Run as root BEFORE exporting the OVA.
# Refuses to pass if any ship-prep step was missed.
set -u

errs=0
ok()   { echo "  OK   $1"; }
bad()  { echo "  FAIL $1"; errs=$((errs+1)); }
check() { if eval "$1" >/dev/null 2>&1; then ok "$2"; else bad "$2"; fi; }

echo "=== ZenPlus ship-ready verification ==="
echo
echo "[systemd units enabled]"
check '[ "$(systemctl is-enabled zenplus-first-boot.service)" = enabled ]' 'zenplus-first-boot enabled'
check '[ "$(systemctl is-enabled zenplus-api.service)" = enabled ]'        'zenplus-api enabled'
check '[ "$(systemctl is-enabled zenplus-poller.service)" = enabled ]'     'zenplus-poller enabled'
check '[ "$(systemctl is-enabled zenplus-dashboard.service)" = enabled ]'  'zenplus-dashboard enabled'
check 'test -L /etc/systemd/system/multi-user.target.wants/zenplus-first-boot.service' 'first-boot symlink present'

echo
echo "[state files cleared]"
check '[ ! -f /opt/zenplus/.env ]'                '.env deleted'
check '[ ! -f /var/lib/zenplus/.initialized ]'    'sentinel deleted'
check '[ ! -s /var/log/zenplus/first-boot.log ] || [ ! -f /var/log/zenplus/first-boot.log ]' 'first-boot log empty/absent'

echo
echo "[services stopped]"
check '! systemctl is-active --quiet zenplus-api.service'       'zenplus-api stopped'
check '! systemctl is-active --quiet zenplus-poller.service'    'zenplus-poller stopped'
check '! systemctl is-active --quiet zenplus-dashboard.service' 'zenplus-dashboard stopped'
check '! docker ps --format "{{.Names}}" | grep -q zenplus-clickhouse' 'clickhouse container stopped'

echo
echo "[session artifacts cleared]"
check '[ ! -s /home/zpsupport/.bash_history ]'    'zpsupport bash history empty'
check '[ ! -s /root/.bash_history ]'              'root bash history empty'
check '[ ! -d /home/zpsupport/.claude ]'          'claude session state removed'

echo
if [ $errs -eq 0 ]; then
    echo "=== READY TO SNAPSHOT ==="
    exit 0
else
    echo "=== NOT READY: $errs failure(s) — fix above before exporting OVA ==="
    exit 1
fi
