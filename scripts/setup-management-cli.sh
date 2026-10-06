#!/bin/bash
# Install the root-owned operator CLI on fresh installs and OTA upgrades.
set -euo pipefail
test "$(id -u)" = 0
ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
install -o root -g root -m 0755 "$ZENPLUS_DIR/bin/zenplus-management" /usr/local/bin/zenplus
echo '[setup-management-cli] Installed operator CLI with signed OTA updates'
