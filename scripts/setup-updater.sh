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
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[setup]${NC} $1"; }
warn() { echo -e "${YELLOW}[setup]${NC} $1"; }

echo ""
echo "============================================"
echo "  ZenPlus OTA Updater Setup"
echo "============================================"
echo ""

# ─── 1. Python dependencies ──────────────────────────────────────────────────
log "Checking Python dependencies ..."
if [ -f "${VENV}/bin/pip" ]; then
    "${VENV}/bin/pip" install -q cryptography httpx openpyxl 2>/dev/null
    if [ -f "${ZENPLUS_DIR}/server/requirements.txt" ]; then
        "${VENV}/bin/pip" install -q -r "${ZENPLUS_DIR}/server/requirements.txt" 2>/dev/null
    fi
    log "Python deps OK"
fi

# ─── 2. Create updater directories ───────────────────────────────────────────
log "Creating updater directories ..."
mkdir -p "${ZENPLUS_DIR}/updater/"{config,logs,backups,keys}

# ─── 3. Default agent.conf if missing ────────────────────────────────────────
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
    log "agent.conf exists, keeping credentials"
fi

# ─── 4. Install systemd units ────────────────────────────────────────────────
log "Installing systemd units ..."
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.service" /etc/systemd/system/
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.timer" /etc/systemd/system/

# ─── 5. Install polkit rule (detect version) ─────────────────────────────────
log "Installing polkit rule ..."
POLKIT_VER=$(pkaction --version 2>/dev/null | grep -oP '[\d.]+' || echo "0.0")
POLKIT_MINOR=$(echo "$POLKIT_VER" | cut -d. -f2)

if [ "${POLKIT_MINOR:-0}" -ge 106 ] 2>/dev/null; then
    mkdir -p /etc/polkit-1/rules.d
    cp "${ZENPLUS_DIR}/updater/polkit/50-zenplus-updater.rules" /etc/polkit-1/rules.d/
    log "Installed .rules (polkit >= 0.106)"
else
    mkdir -p /etc/polkit-1/localauthority/50-local.d
    cp "${ZENPLUS_DIR}/updater/polkit/zenplus-updater.pkla" /etc/polkit-1/localauthority/50-local.d/
    log "Installed .pkla (polkit < 0.106)"
fi
systemctl restart polkit 2>/dev/null || true

# ─── 6. Fix NoNewPrivileges on zenplus-api ────────────────────────────────────
if grep -q "NoNewPrivileges=true" /etc/systemd/system/zenplus-api.service 2>/dev/null; then
    log "Fixing NoNewPrivileges on zenplus-api ..."
    sed -i 's/NoNewPrivileges=true/NoNewPrivileges=false/' /etc/systemd/system/zenplus-api.service
fi

# ─── 7. Reload systemd and enable timer ──────────────────────────────────────
log "Reloading systemd ..."
systemctl daemon-reload
systemctl enable zenplus-updater.timer
systemctl restart zenplus-updater.timer

# ─── 8. Rebuild dashboard ────────────────────────────────────────────────────
if [ -f "${ZENPLUS_DIR}/dashboard/package.json" ]; then
    log "Rebuilding dashboard ..."
    cd "${ZENPLUS_DIR}/dashboard"
    npm install --silent 2>/dev/null
    npm run build 2>/dev/null
    cd "${ZENPLUS_DIR}"
    log "Dashboard built"
fi

# ─── 9. Restart services ─────────────────────────────────────────────────────
log "Restarting services ..."
systemctl restart zenplus-api nginx 2>/dev/null || true
systemctl restart zenplus-poller 2>/dev/null || true

# ─── 10. Verify ──────────────────────────────────────────────────────────────
echo ""
log "Verifying ..."
for svc in zenplus-api zenplus-poller nginx zenplus-updater.timer; do
    st=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    if [ "$st" = "active" ] || [ "$st" = "waiting" ]; then
        echo -e "  ${GREEN}✓${NC} $svc"
    else
        echo -e "  ${RED}✗${NC} $svc ($st)"
    fi
done

APPLIANCE_ID=$(grep -Po '(?<=^id = ).*' "${AGENT_CONF}" 2>/dev/null || echo "")
echo ""
if [ -n "${APPLIANCE_ID}" ]; then
    echo -e "  ${GREEN}✓${NC} Registered: ${APPLIANCE_ID}"
else
    echo -e "  ${YELLOW}!${NC} Not registered"
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
if [ -z "${APPLIANCE_ID}" ]; then
echo "  Open dashboard > Settings > Updates"
echo "  Enter your license key to register"
else
echo "  Ready for OTA updates (every 4h)"
fi
echo "============================================"
echo ""
