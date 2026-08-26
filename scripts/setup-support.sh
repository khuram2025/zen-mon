#!/bin/bash
# ============================================================================
# ZenPlus Tech-Support Bundle Setup
#
# Installs the unprivileged systemd queue worker, cleanup timer, and runtime
# directories that the
# Support tab in the dashboard uses to generate diagnostic bundles.
#
# Run this once per appliance, after the OTA updater is in place.
#   sudo bash scripts/setup-support.sh
# ============================================================================
set -e

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
ZENPLUS_SOURCE_DIR="${ZENPLUS_SOURCE_DIR:-${ZENPLUS_DIR}}"
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
for runtime_dir in \
    "${SUPPORT_DIR}/requests" \
    "${SUPPORT_DIR}/jobs" \
    "${SUPPORT_DIR}/bundles"; do
    if [ -L "${runtime_dir}" ]; then
        err "refusing symlinked support runtime directory: ${runtime_dir}"
        exit 1
    fi
done
mkdir -p \
    "${SUPPORT_DIR}/requests" \
    "${SUPPORT_DIR}/jobs" \
    "${SUPPORT_DIR}/bundles"

# The API, worker, and cleanup task all run as zenplus. Keeping the worker
# unprivileged is essential because the application tree and venv are also
# owned by that account on standard installations.
if getent group zenplus >/dev/null 2>&1; then
    chown -R zenplus:zenplus \
        "${SUPPORT_DIR}/requests" "${SUPPORT_DIR}/jobs" "${SUPPORT_DIR}/bundles"
    chmod 0750 "${SUPPORT_DIR}/requests" "${SUPPORT_DIR}/jobs" "${SUPPORT_DIR}/bundles"
else
    warn "zenplus group missing — leaving ownership unchanged"
fi

# ─── 2. Systemd units ────────────────────────────────────────────────────────
log "Installing systemd units ..."
install -o root -g root -m 0644 \
    "${ZENPLUS_SOURCE_DIR}/updater/systemd/zenplus-support-dispatch.service" \
    /etc/systemd/system/zenplus-support-dispatch.service
install -o root -g root -m 0644 \
    "${ZENPLUS_SOURCE_DIR}/updater/systemd/zenplus-support-queue.path" \
    /etc/systemd/system/zenplus-support-queue.path
install -o root -g root -m 0644 \
    "${ZENPLUS_SOURCE_DIR}/updater/systemd/zenplus-support-cleanup.service" \
    /etc/systemd/system/zenplus-support-cleanup.service
install -o root -g root -m 0644 \
    "${ZENPLUS_SOURCE_DIR}/updater/systemd/zenplus-support-cleanup.timer" \
    /etc/systemd/system/zenplus-support-cleanup.timer

# ─── 3. Stable unprivileged dispatcher + legacy-grant removal ───────────────
DISPATCH_SRC="${ZENPLUS_SOURCE_DIR}/scripts/zenplus-support-dispatch"
DISPATCH_DST="/usr/local/libexec/zenplus-support-dispatch"
if [ ! -f "${DISPATCH_SRC}" ]; then
    err "support dispatcher missing: ${DISPATCH_SRC}"
    exit 1
fi
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${DISPATCH_SRC}" "${DISPATCH_DST}"

# Remove grants shipped by older releases. The legacy .pkla form allowed the
# service account to manage every systemd unit and must not survive upgrades.
systemctl stop 'zenplus-support-bundle@*.service' 2>/dev/null || true
rm -f \
    /etc/systemd/system/zenplus-support-bundle@.service \
    /etc/sudoers.d/zenplus-support \
    /usr/local/libexec/zenplus-support-start \
    /etc/polkit-1/rules.d/51-zenplus-support.rules \
    /etc/polkit-1/localauthority/50-local.d/zenplus-support.pkla

# ─── 4. Reload + enable queue watcher and cleanup timer ─────────────────────
log "Reloading systemd ..."
systemctl daemon-reload
systemctl enable --now zenplus-support-queue.path
systemctl enable --now zenplus-support-cleanup.timer

# ─── 5. Verify ───────────────────────────────────────────────────────────────
echo ""
log "Verifying ..."
for path in \
    /etc/systemd/system/zenplus-support-dispatch.service \
    /etc/systemd/system/zenplus-support-queue.path \
    /etc/systemd/system/zenplus-support-cleanup.timer \
    "${DISPATCH_DST}" \
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
