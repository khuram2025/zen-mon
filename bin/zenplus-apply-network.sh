#!/bin/bash
# /opt/zenplus/bin/zenplus-apply-network.sh
# Apply systemd-networkd config for an interface.
# Reads new config content from stdin. Backs up the existing file,
# writes the new one, fixes ownership/perms, restarts systemd-networkd.
set -euo pipefail

iface="${1:-}"
if ! [[ "$iface" =~ ^[a-zA-Z0-9_-]{1,15}$ ]]; then
    echo "Invalid interface name: ${iface}" >&2
    exit 2
fi

target="/etc/systemd/network/10-${iface}.network"
backup="${target}.bak"

if [ -f "$target" ]; then
    cp -a "$target" "$backup"
fi

# Read entire stdin into target
cat > "$target"
chmod 644 "$target"
chown root:root "$target"

systemctl restart systemd-networkd
