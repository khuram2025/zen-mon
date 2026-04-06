#!/bin/bash
# Setup/repair the OTA update agent on this appliance.
# Run after git pull or as a post-update hook.
set -e

ZENPLUS_DIR="/opt/zenplus"

echo "[setup-updater] Installing systemd units ..."
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.service" /etc/systemd/system/
cp "${ZENPLUS_DIR}/updater/systemd/zenplus-updater.timer" /etc/systemd/system/

echo "[setup-updater] Installing polkit rule ..."
mkdir -p /etc/polkit-1/rules.d
cp "${ZENPLUS_DIR}/updater/polkit/50-zenplus-updater.rules" /etc/polkit-1/rules.d/

echo "[setup-updater] Reloading systemd ..."
systemctl daemon-reload
systemctl enable zenplus-updater.timer
systemctl restart zenplus-updater.timer

# Ensure NoNewPrivileges is disabled on zenplus-api (needed for polkit)
if grep -q "NoNewPrivileges=true" /etc/systemd/system/zenplus-api.service 2>/dev/null; then
    echo "[setup-updater] Fixing NoNewPrivileges on zenplus-api ..."
    sed -i 's/NoNewPrivileges=true/NoNewPrivileges=false/' /etc/systemd/system/zenplus-api.service
    systemctl daemon-reload
    systemctl restart zenplus-api
fi

echo "[setup-updater] Done."
