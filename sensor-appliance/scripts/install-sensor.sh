#!/usr/bin/env bash
set -euo pipefail

BIN_SRC="${ZENPLUS_SENSOR_BIN:-/tmp/zenplus-sensor}"
ENV_SRC="${ZENPLUS_SENSOR_ENV:-/tmp/sensor.env}"

if [[ ! -x "$BIN_SRC" ]]; then
  echo "missing executable sensor binary: $BIN_SRC" >&2
  exit 1
fi

if ! getent group zenplus-sensor >/dev/null; then
  groupadd --system zenplus-sensor
fi
if ! id zenplus-sensor >/dev/null 2>&1; then
  useradd --system --home /var/lib/zenplus-sensor --shell /usr/sbin/nologin --gid zenplus-sensor zenplus-sensor
fi

# The runtime atomically rewrites sensor.env after enrollment to remove the
# one-time token, so its private group needs write permission on this directory.
install -d -m 0770 -o root -g zenplus-sensor /etc/zenplus-sensor
# The executable lives inside the sensor-owned state tree. This is the only
# writable executable location exposed to the service and allows a verified
# update to use an atomic same-filesystem rename without granting broader
# filesystem write access.
install -d -m 0700 -o zenplus-sensor -g zenplus-sensor /var/lib/zenplus-sensor
install -d -m 0700 -o zenplus-sensor -g zenplus-sensor /var/lib/zenplus-sensor/bin
install -d -m 0750 -o zenplus-sensor -g zenplus-sensor /var/log/zenplus-sensor

install -m 0700 -o zenplus-sensor -g zenplus-sensor "$BIN_SRC" /var/lib/zenplus-sensor/bin/zenplus-sensor
if [[ -f "$ENV_SRC" ]]; then
  install -m 0600 -o zenplus-sensor -g zenplus-sensor "$ENV_SRC" /etc/zenplus-sensor/sensor.env
else
  install -m 0600 -o zenplus-sensor -g zenplus-sensor /opt/zenplus-sensor/config/zenplus-sensor.env.example /etc/zenplus-sensor/sensor.env
fi
install -m 0644 /opt/zenplus-sensor/systemd/zenplus-sensor.service /etc/systemd/system/zenplus-sensor.service

chown -R zenplus-sensor:zenplus-sensor /var/lib/zenplus-sensor /var/log/zenplus-sensor
chmod 0700 /var/lib/zenplus-sensor /var/lib/zenplus-sensor/bin /var/lib/zenplus-sensor/bin/zenplus-sensor
systemctl daemon-reload
systemctl enable zenplus-sensor.service

echo "ZenPlus sensor installed. Configure /etc/zenplus-sensor/sensor.env, then run:"
echo "  systemctl start zenplus-sensor"
