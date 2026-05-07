#!/usr/bin/env bash
set -euo pipefail

BIN_SRC="${ZENPLUS_SENSOR_BIN:-/tmp/zenplus-sensor}"
ENV_SRC="${ZENPLUS_SENSOR_ENV:-/tmp/sensor.env}"

if [[ ! -x "$BIN_SRC" ]]; then
  echo "missing executable sensor binary: $BIN_SRC" >&2
  exit 1
fi

install -d -m 0750 -o root -g root /etc/zenplus-sensor
install -d -m 0750 -o root -g root /var/lib/zenplus-sensor
install -d -m 0750 -o root -g root /var/log/zenplus-sensor

if ! getent group zenplus-sensor >/dev/null; then
  groupadd --system zenplus-sensor
fi
if ! id zenplus-sensor >/dev/null 2>&1; then
  useradd --system --home /var/lib/zenplus-sensor --shell /usr/sbin/nologin --gid zenplus-sensor zenplus-sensor
fi

install -m 0755 "$BIN_SRC" /usr/local/bin/zenplus-sensor
if [[ -f "$ENV_SRC" ]]; then
  install -m 0640 -o root -g zenplus-sensor "$ENV_SRC" /etc/zenplus-sensor/sensor.env
else
  install -m 0640 -o root -g zenplus-sensor /opt/zenplus-sensor/config/zenplus-sensor.env.example /etc/zenplus-sensor/sensor.env
fi
install -m 0644 /opt/zenplus-sensor/systemd/zenplus-sensor.service /etc/systemd/system/zenplus-sensor.service

chown -R zenplus-sensor:zenplus-sensor /var/lib/zenplus-sensor /var/log/zenplus-sensor
systemctl daemon-reload
systemctl enable zenplus-sensor.service

echo "ZenPlus sensor installed. Configure /etc/zenplus-sensor/sensor.env, then run:"
echo "  systemctl start zenplus-sensor"
