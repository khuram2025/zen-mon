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

for value in "$SERVER_URL" "$SENSOR_NAME" "$ENROLLMENT_TOKEN"; do
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "setup values must not contain newlines" >&2
    exit 1
  fi
done

install -d -m 0770 -o root -g zenplus-sensor /etc/zenplus-sensor
install -d -m 0700 -o zenplus-sensor -g zenplus-sensor /var/lib/zenplus-sensor /var/lib/zenplus-sensor/bin
: > "$ENV_FILE"
write_env_line() {
  local key="$1" value="$2"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s="%s"\n' "$key" "$value" >> "$ENV_FILE"
}
write_env_line ZENPLUS_SERVER_URL "$SERVER_URL"
write_env_line ZENPLUS_ENROLLMENT_TOKEN "$ENROLLMENT_TOKEN"
write_env_line ZENPLUS_SENSOR_NAME "$SENSOR_NAME"
write_env_line ZENPLUS_VERIFY_TLS 1
write_env_line ZENPLUS_SENSOR_STATE_DIR /var/lib/zenplus-sensor
write_env_line ZENPLUS_SENSOR_ENV_FILE /etc/zenplus-sensor/sensor.env
write_env_line ZENPLUS_SENSOR_LOG_LEVEL info
write_env_line ZENPLUS_HEARTBEAT_INTERVAL_SECONDS 30
write_env_line ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS 60
write_env_line ZENPLUS_UPLOAD_INTERVAL_SECONDS 10
write_env_line ZENPLUS_MAX_WORKERS 100
write_env_line ZENPLUS_SPOOL_MAX_MB 512
write_env_line ZENPLUS_SPOOL_RETENTION_HOURS 72
chown zenplus-sensor:zenplus-sensor "$ENV_FILE"
chmod 0600 "$ENV_FILE"

systemctl enable --now zenplus-sensor.service
echo "Sensor service started. Check status with: systemctl status zenplus-sensor"
