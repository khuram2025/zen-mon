#!/bin/bash
# ============================================================================
# ZenPlus OTA Updater Setup
#
# Prepares an existing appliance for OTA updates from zentryc.com.
# Run this after pulling the latest code from GitHub.
#
# Usage:
#   cd /opt/zenplus
#   sudo git pull origin main
#   sudo bash scripts/setup-updater.sh
#
# After running, go to Settings > Updates in the dashboard to register
# this appliance with your license key.
# ============================================================================
set -e

ZENPLUS_DIR="/opt/zenplus"
VENV="${ZENPLUS_DIR}/venv"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[setup]${NC} $1"; }
warn() { echo -e "${YELLOW}[setup]${NC} $1"; }
err()  { echo -e "${RED}[setup]${NC} $1"; }

echo ""
echo "============================================"
echo "  ZenPlus OTA Updater Setup"
echo "============================================"
echo ""

# ─── 1. Install Python dependencies ──────────────────────────────────────────
log "Checking Python dependencies ..."
if [ -f "${VENV}/bin/pip" ]; then
    "${VENV}/bin/pip" install -q cryptography httpx 2>/dev/null
    log "Python deps OK"
else
    err "Virtual env not found at ${VENV}. Skipping pip install."
fi

# ─── 2. Install pip requirements if requirements.txt changed ──────────────────
if [ -f "${ZENPLUS_DIR}/server/requirements.txt" ]; then
    log "Installing server requirements ..."
    "${VENV}/bin/pip" install -q -r "${ZENPLUS_DIR}/server/requirements.txt" 2>/dev/null
fi

# ─── 3. Create updater directories ───────────────────────────────────────────
log "Creating updater directories ..."
mkdir -p "${ZENPLUS_DIR}/updater/"{config,logs,backups,keys}

# ─── 4. Create default agent.conf if missing ─────────────────────────────────
AGENT_CONF="${ZENPLUS_DIR}/updater/config/agent.conf"
if [ ! -f "${AGENT_CONF}" ]; then
    log "Creating default agent.conf ..."
    cat > "${AGENT_CONF}" << 'CONF'
[server]
url = https://zentryc.com
check_interval_seconds = 14400
download_timeout_seconds = 600

[appliance]
id =
api_key =

[security]
public_key_path = /opt/zenplus/updater/keys/zentryc-release.pub
max_manifest_age_days = 30
verify_tls = true

[update]
backup_dir = /opt/zenplus/updater/backups
max_backups = 3
auto_update = true
maintenance_window_start =
maintenance_window_end =

[logging]
log_file = /opt/zenplus/updater/logs/update.log
log_level = INFO
max_log_size_mb = 10
log_rotate_count = 5
CONF
    chmod 600 "${AGENT_CONF}"
else
    log "agent.conf already exists, keeping current credentials"
fi

# ─── 5. Install systemd units ────────────────────────────────────────────────
log "Installing systemd units ..."
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.service" /etc/systemd/system/
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.timer" /etc/systemd/system/

# ─── 6. Install polkit rule ──────────────────────────────────────────────────
log "Installing polkit rule ..."
mkdir -p /etc/polkit-1/rules.d
cp "${ZENPLUS_DIR}/updater/polkit/50-zenplus-updater.rules" /etc/polkit-1/rules.d/

# ─── 7. Fix NoNewPrivileges on zenplus-api ────────────────────────────────────
if grep -q "NoNewPrivileges=true" /etc/systemd/system/zenplus-api.service 2>/dev/null; then
    log "Fixing NoNewPrivileges on zenplus-api ..."
    sed -i 's/NoNewPrivileges=true/NoNewPrivileges=false/' /etc/systemd/system/zenplus-api.service
    RESTART_API=1
fi

# ─── 8. Reload systemd and enable timer ──────────────────────────────────────
log "Reloading systemd ..."
systemctl daemon-reload
systemctl enable zenplus-updater.timer
systemctl restart zenplus-updater.timer

# ─── 9. Rebuild dashboard ────────────────────────────────────────────────────
DASHBOARD_DIR="${ZENPLUS_DIR}/dashboard"
if [ -f "${DASHBOARD_DIR}/package.json" ]; then
    log "Rebuilding dashboard ..."
    cd "${DASHBOARD_DIR}"
    npm install --silent 2>/dev/null
    npm run build 2>/dev/null
    log "Dashboard built"
    cd "${ZENPLUS_DIR}"
fi

# ─── 10. Restart services ────────────────────────────────────────────────────
log "Restarting services ..."
systemctl restart zenplus-api 2>/dev/null || warn "zenplus-api restart failed"
systemctl restart zenplus-poller 2>/dev/null || warn "zenplus-poller restart failed"
systemctl restart nginx 2>/dev/null || warn "nginx restart failed"

if [ "${RESTART_API}" = "1" ]; then
    sleep 1
    systemctl restart zenplus-api 2>/dev/null
fi

# ─── 11. Verify ──────────────────────────────────────────────────────────────
echo ""
log "Verifying services ..."
for svc in zenplus-api zenplus-poller nginx zenplus-updater.timer; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    if [ "$status" = "active" ] || [ "$status" = "waiting" ]; then
        echo -e "  ${GREEN}✓${NC} $svc ($status)"
    else
        echo -e "  ${RED}✗${NC} $svc ($status)"
    fi
done

# Check registration
APPLIANCE_ID=$(grep -Po '(?<=^id = ).*' "${AGENT_CONF}" 2>/dev/null || echo "")
echo ""
if [ -n "${APPLIANCE_ID}" ]; then
    echo -e "  ${GREEN}✓${NC} Registered: ${APPLIANCE_ID}"
else
    echo -e "  ${YELLOW}!${NC} Not registered — go to Settings > Updates to enter your license key"
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
if [ -z "${APPLIANCE_ID}" ]; then
echo "  1. Open the dashboard in your browser"
echo "  2. Go to Settings > Updates tab"
echo "  3. Enter your license key and click Register"
echo "  4. Updates will flow automatically"
else
echo "  Appliance is registered and ready for OTA updates."
echo "  Updates check every 4 hours automatically."
fi
echo "============================================"
echo ""
