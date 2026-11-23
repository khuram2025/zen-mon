#!/bin/bash
# Run from the verified release source, including on a supported legacy appliance.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
units=/etc/systemd/system
install -D -o root -g root -m 0644 "$root/updater/schedule.py" /usr/local/libexec/zenplus-updater-schedule.py
install -m 0644 "$root/updater/systemd/zenplus-updater-apt.conf" /usr/local/libexec/zenplus-updater-apt.conf
if [ -d /etc/needrestart/conf.d ]; then
    install -m 0644 "$root/updater/systemd/zenplus-updater-needrestart.conf" /etc/needrestart/conf.d/zenplus-updater.conf
fi
install -d -m 0755 "$units/zenplus-updater.service.d"
install -m 0644 "$root/updater/systemd/maintenance-schedule.conf" "$units/zenplus-updater.service.d/maintenance-schedule.conf"
for unit in zenplus-update-window.path zenplus-update-window.service; do
    install -m 0644 "$root/updater/systemd/$unit" "$units/$unit"
done
systemctl daemon-reload
systemctl enable --now zenplus-update-window.path
systemctl start zenplus-update-window.service
