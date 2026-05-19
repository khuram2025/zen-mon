#!/bin/bash
# ============================================================================
# ZenPlus Tech-Support Bundle Setup
#
# Installs the systemd unit, sudoers grant, and runtime directories that the
# Support tab in the dashboard uses to generate diagnostic bundles.
#
# Run this once per appliance, after the OTA updater is in place.
#   sudo bash scripts/setup-support.sh
# ============================================================================
set -e

ZENPLUS_DIR="/opt/zenplus"
VENV="${ZENPLUS_DIR}/venv"
SUPPORT_DIR="${ZENPLUS_DIR}/support"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[setup-support]${NC} $1"; }
warn() { echo -e "${YELLOW}[setup-support]${NC} $1"; }
err()  { echo -e "${RED}[setup-support]${NC} $1" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    err "must run as root"
    exit 1
fi

echo ""
echo "============================================"
echo "  ZenPlus Tech-Support Bundle Setup"
echo "============================================"
echo ""

# ─── 1. Runtime directories ──────────────────────────────────────────────────
log "Creating runtime directories ..."
mkdir -p \
    "${SUPPORT_DIR}/requests" \
    "${SUPPORT_DIR}/jobs" \
    "${SUPPORT_DIR}/bundles"

# requests + jobs are written by the API (zenplus user). bundles are written
# by the worker (root) and need to be readable by the zenplus group so the
# API process can stream the file.
if getent group zenplus >/dev/null 2>&1; then
    chown -R zenplus:zenplus "${SUPPORT_DIR}/requests" "${SUPPORT_DIR}/jobs"
    chown root:zenplus "${SUPPORT_DIR}/bundles"
    chmod 0750 "${SUPPORT_DIR}/requests" "${SUPPORT_DIR}/jobs" "${SUPPORT_DIR}/bundles"
else
    warn "zenplus group missing — leaving ownership unchanged"
fi

# ─── 2. Systemd units ────────────────────────────────────────────────────────
log "Installing systemd units ..."
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-support-bundle@.service"   /etc/systemd/system/
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-support-cleanup.service"   /etc/systemd/system/
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-support-cleanup.timer"     /etc/systemd/system/

# ─── 3. Sudoers grant (validated) ────────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/zenplus-support"
SUDOERS_TMP="$(mktemp)"
cat > "${SUDOERS_TMP}" <<'EOF'
# Allow the zenplus user to start support-bundle worker instances without a
# password. The instance name (after the @) is a UUID validated by the worker
# itself, so the wildcard cannot escape /opt/zenplus/support/.
zenplus ALL=(root) NOPASSWD: /bin/systemctl start zenplus-support-bundle@*.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl --no-block start zenplus-support-bundle@*.service
EOF
chmod 0440 "${SUDOERS_TMP}"

if visudo -cf "${SUDOERS_TMP}" >/dev/null; then
    mv "${SUDOERS_TMP}" "${SUDOERS_FILE}"
    log "Installed sudoers grant: ${SUDOERS_FILE}"
else
    err "sudoers syntax invalid — refusing to install"
    rm -f "${SUDOERS_TMP}"
    exit 1
fi

# ─── 4. Polkit rule for the API process (mirrors updater pattern) ────────────
log "Installing polkit rule ..."
POLKIT_VER=$(pkaction --version 2>/dev/null | grep -oP '[\d.]+' || echo "0.0")
POLKIT_MINOR=$(echo "$POLKIT_VER" | cut -d. -f2)

if [ "${POLKIT_MINOR:-0}" -ge 106 ] 2>/dev/null; then
    mkdir -p /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/51-zenplus-support.rules <<'EOF'
// Allow the zenplus user to start support-bundle worker instances without
// being prompted by polkit. The @<uuid> is validated by the worker.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "zenplus") {
        var unit = action.lookup("unit");
        if (unit && unit.indexOf("zenplus-support-bundle@") === 0) {
            return polkit.Result.YES;
        }
    }
});
EOF
    log "Installed .rules (polkit >= 0.106)"
else
    mkdir -p /etc/polkit-1/localauthority/50-local.d
    cat > /etc/polkit-1/localauthority/50-local.d/zenplus-support.pkla <<'EOF'
[zenplus support bundle]
Identity=unix-user:zenplus
Action=org.freedesktop.systemd1.manage-units
ResultActive=yes
EOF
    log "Installed .pkla (polkit < 0.106)"
fi
systemctl restart polkit 2>/dev/null || true

# ─── 5. Reload + enable cleanup timer ────────────────────────────────────────
log "Reloading systemd ..."
systemctl daemon-reload
systemctl enable zenplus-support-cleanup.timer
systemctl restart zenplus-support-cleanup.timer

# ─── 6. Verify ───────────────────────────────────────────────────────────────
echo ""
log "Verifying ..."
for path in \
    /etc/systemd/system/zenplus-support-bundle@.service \
    /etc/systemd/system/zenplus-support-cleanup.timer \
    "${SUDOERS_FILE}" \
    "${SUPPORT_DIR}/requests" \
    "${SUPPORT_DIR}/jobs" \
    "${SUPPORT_DIR}/bundles"; do
    if [ -e "$path" ]; then
        echo -e "  ${GREEN}✓${NC} $path"
    else
        echo -e "  ${RED}✗${NC} $path (missing)"
    fi
done

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Open dashboard > Settings > General > Support"
echo "  to generate a tech-support bundle."
echo ""
