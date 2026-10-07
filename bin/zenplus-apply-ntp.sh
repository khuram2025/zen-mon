#!/bin/bash
# /opt/zenplus/bin/zenplus-apply-ntp.sh
# Set the NTP servers used by systemd-timesyncd and make the change persistent.
# Arguments: one to four hostnames or IP addresses (validated here, because this
# script runs as root through sudo).
set -euo pipefail

CONF_DIR="${ZENPLUS_TIMESYNCD_DIR:-/etc/systemd/timesyncd.conf.d}"

if [ "$#" -lt 1 ] || [ "$#" -gt 4 ]; then
    echo "usage: zenplus-apply-ntp.sh <server> [server2 ... server4]" >&2
    exit 2
fi

for server in "$@"; do
    if [ "${#server}" -gt 253 ] \
        || ! [[ "$server" =~ ^[A-Za-z0-9]([A-Za-z0-9._:-]*[A-Za-z0-9])?$ ]]; then
        echo "Invalid NTP server: ${server}" >&2
        exit 2
    fi
done

install -d -m 0755 "$CONF_DIR"
chown root:root "$CONF_DIR"
tmp=$(mktemp "${CONF_DIR}/.10-zenplus.conf.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf '[Time]\nNTP=%s\n' "$*" > "$tmp"
chmod 0644 "$tmp"
chown root:root "$tmp"
mv "$tmp" "${CONF_DIR}/10-zenplus.conf"
trap - EXIT

timedatectl set-ntp true
systemctl restart systemd-timesyncd
