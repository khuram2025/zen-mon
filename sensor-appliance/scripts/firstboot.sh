#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/zenplus-sensor/sensor.env"

if [[ -s "$ENV_FILE" ]] && grep -q '^ZENPLUS_SERVER_URL=' "$ENV_FILE"; then
  systemctl enable --now zenplus-sensor.service || true
  exit 0
fi

echo "ZenPlus Remote Sensor first-boot setup"
echo
read -r -p "Controller URL (https://controller.example.com): " SERVER_URL
read -r -p "Sensor name (branch-01): " SENSOR_NAME
read -r -s -p "Enrollment token: " ENROLLMENT_TOKEN
echo

if [[ -z "$SERVER_URL" || -z "$SENSOR_NAME" || -z "$ENROLLMENT_TOKEN" ]]; then
  echo "controller URL, sensor name, and enrollment token are required" >&2
  exit 1
fi

install -d -m 0750 -o root -g zenplus-sensor /etc/zenplus-sensor
cat > "$ENV_FILE" <<EOF
ZENPLUS_SERVER_URL=$SERVER_URL
ZENPLUS_ENROLLMENT_TOKEN=$ENROLLMENT_TOKEN
ZENPLUS_SENSOR_NAME=$SENSOR_NAME
ZENPLUS_VERIFY_TLS=1
ZENPLUS_SENSOR_STATE_DIR=/var/lib/zenplus-sensor
ZENPLUS_SENSOR_LOG_LEVEL=info
ZENPLUS_HEARTBEAT_INTERVAL_SECONDS=30
ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS=60
ZENPLUS_UPLOAD_INTERVAL_SECONDS=10
ZENPLUS_MAX_WORKERS=100
ZENPLUS_SPOOL_MAX_MB=512
ZENPLUS_SPOOL_RETENTION_HOURS=72
EOF
chown root:zenplus-sensor "$ENV_FILE"
chmod 0640 "$ENV_FILE"

systemctl enable --now zenplus-sensor.service
echo "Sensor service started. Check status with: systemctl status zenplus-sensor"
