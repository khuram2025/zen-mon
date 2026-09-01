#!/usr/bin/env bash
set -euo pipefail

CONFIG_JSON=""
BASE_QCOW2="${ZENPLUS_SENSOR_BASE_QCOW2:-/opt/zenplus/sensor-appliance/out/real-ova/zenplus-sensor.qcow2}"
BASE_OVF="${ZENPLUS_SENSOR_BASE_OVF:-/opt/zenplus/sensor-appliance/out/real-ova/zenplus-sensor.ovf}"
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config-json) CONFIG_JSON="${2:-}"; shift 2 ;;
    --base-qcow2) BASE_QCOW2="${2:-}"; shift 2 ;;
    --base-ovf) BASE_OVF="${2:-}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$CONFIG_JSON" || -z "$OUT_DIR" ]]; then
  echo "usage: $0 --config-json /path/config.json --out-dir /path/out" >&2
  exit 2
fi
if [[ ! -f "$CONFIG_JSON" ]]; then
  echo "missing config json: $CONFIG_JSON" >&2
  exit 1
fi
if [[ ! -f "$BASE_QCOW2" ]]; then
  echo "missing base qcow2: $BASE_QCOW2" >&2
  exit 1
fi
if [[ ! -f "$BASE_OVF" ]]; then
  echo "missing base ovf: $BASE_OVF" >&2
  exit 1
fi

command -v qemu-img >/dev/null 2>&1 || { echo "missing qemu-img" >&2; exit 1; }
command -v virt-customize >/dev/null 2>&1 || { echo "missing virt-customize" >&2; exit 1; }

install -d -m 0750 "$OUT_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python3 - "$CONFIG_JSON" "$TMP" <<'PY'
import ipaddress
import json
import re
import shlex
import sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text())
tmp = Path(sys.argv[2])

name = cfg["sensor_name"]
server_url = cfg["server_url"]
token = cfg["enrollment_token"]
proxy_url = cfg.get("proxy_url") or ""
controller_ca_pem = cfg.get("controller_ca_pem") or ""

if controller_ca_pem:
    if len(controller_ca_pem) > 128_000 or "-----BEGIN CERTIFICATE-----" not in controller_ca_pem:
        raise SystemExit("invalid controller CA certificate")
    (tmp / "zenplus-controller.crt").write_text(controller_ca_pem.rstrip() + "\n")

def env_line(key, value):
    value = str(value)
    if any(char in value for char in "\r\n\x00"):
        raise SystemExit(f"{key} must be a single-line environment value")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'

env_values = [
    ("ZENPLUS_SERVER_URL", server_url),
    ("ZENPLUS_ENROLLMENT_TOKEN", token),
    ("ZENPLUS_SENSOR_NAME", name),
    ("ZENPLUS_VERIFY_TLS", 1),
    ("ZENPLUS_SENSOR_STATE_DIR", "/var/lib/zenplus-sensor"),
    ("ZENPLUS_SENSOR_ENV_FILE", "/etc/zenplus-sensor/sensor.env"),
    ("ZENPLUS_HEARTBEAT_INTERVAL_SECONDS", 30),
    ("ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS", 60),
    ("ZENPLUS_UPLOAD_INTERVAL_SECONDS", 10),
    ("ZENPLUS_MAX_WORKERS", 100),
    ("ZENPLUS_SPOOL_MAX_MB", 512),
    ("ZENPLUS_SPOOL_RETENTION_HOURS", 72),
]
if proxy_url:
    env_values.extend([
        ("HTTP_PROXY", proxy_url),
        ("HTTPS_PROXY", proxy_url),
        ("NO_PROXY", "localhost,127.0.0.1,::1"),
    ])
env_lines = [env_line(key, value) for key, value in env_values]
(tmp / "sensor.env").write_text("\n".join(env_lines) + "\n")

network_mode = cfg.get("network_mode") or "dhcp"
if network_mode == "static":
    ip = cfg.get("sensor_ip")
    cidr = int(cfg.get("sensor_cidr") or 0)
    gateway = cfg.get("gateway")
    if not ip or not cidr or not gateway:
        raise SystemExit("static network requires sensor_ip, sensor_cidr, gateway")
    ipaddress.ip_address(ip)
    ipaddress.ip_address(gateway)
    dns = cfg.get("dns_servers") or ["1.1.1.1", "8.8.8.8"]
    for item in dns:
        ipaddress.ip_address(item)
    dns_yaml = ", ".join(dns)
    (tmp / "99-zenplus-sensor.yaml").write_text(f"""network:
  version: 2
  ethernets:
    ens.*:
      match:
        name: "e*"
      dhcp4: false
      dhcp6: false
      addresses: [{ip}/{cidr}]
      routes:
        - to: default
          via: {gateway}
      nameservers:
        addresses: [{dns_yaml}]
""")

if cfg.get("enable_console_user"):
    console_user = cfg.get("console_username") or ""
    console_password = cfg.get("console_password") or ""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", console_user):
        raise SystemExit("invalid console username")
    if len(console_password) < 8 or any(c in console_password for c in "\r\n\x00"):
        raise SystemExit("invalid console password")
    script = f"""#!/bin/sh
set -eu
id {shlex.quote(console_user)} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups sudo {shlex.quote(console_user)}
printf '%s:%s\\n' {shlex.quote(console_user)} {shlex.quote(console_password)} | chpasswd
rm -f /etc/sudoers.d/90-zenplus-console
"""
    (tmp / "configure-console.sh").write_text(script)
    (tmp / "configure-console.sh").chmod(0o700)
PY

WORK_QCOW2="$OUT_DIR/zenplus-sensor-configured.qcow2"
VMDK="$OUT_DIR/zenplus-sensor-disk1.vmdk"
OVF="$OUT_DIR/zenplus-sensor.ovf"
MF="$OUT_DIR/zenplus-sensor.mf"
OVA="$OUT_DIR/zenplus-sensor-configured.ova"

rm -f "$WORK_QCOW2" "$VMDK" "$OVF" "$MF" "$OVA"
cp "$BASE_QCOW2" "$WORK_QCOW2"

export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
VIRT_ARGS=(
  --copy-in "$TMP/sensor.env:/etc/zenplus-sensor" \
  --run-command 'chown root:zenplus-sensor /etc/zenplus-sensor && chmod 0770 /etc/zenplus-sensor' \
  --run-command 'chown zenplus-sensor:zenplus-sensor /etc/zenplus-sensor/sensor.env' \
  --run-command 'chmod 0600 /etc/zenplus-sensor/sensor.env' \
  --run-command 'systemctl enable zenplus-sensor.service'
)
if [[ -f "$TMP/configure-console.sh" ]]; then
  VIRT_ARGS+=(
    --copy-in "$TMP/configure-console.sh:/tmp"
    --run-command 'sh /tmp/configure-console.sh && rm -f /tmp/configure-console.sh'
  )
fi
if [[ -f "$TMP/zenplus-controller.crt" ]]; then
  VIRT_ARGS+=(
    --copy-in "$TMP/zenplus-controller.crt:/usr/local/share/ca-certificates"
    --run-command 'chown root:root /usr/local/share/ca-certificates/zenplus-controller.crt && chmod 0644 /usr/local/share/ca-certificates/zenplus-controller.crt && update-ca-certificates'
  )
fi
virt-customize -a "$WORK_QCOW2" "${VIRT_ARGS[@]}"

if [[ -f "$TMP/99-zenplus-sensor.yaml" ]]; then
  virt-customize -a "$WORK_QCOW2" \
    --copy-in "$TMP/99-zenplus-sensor.yaml:/etc/netplan" \
    --run-command 'chmod 0600 /etc/netplan/99-zenplus-sensor.yaml'
fi

qemu-img convert -O vmdk -o subformat=streamOptimized "$WORK_QCOW2" "$VMDK"
cp "$BASE_OVF" "$OVF"
VMDK_SIZE="$(stat -c%s "$VMDK")"
python3 - "$OVF" "$VMDK_SIZE" <<'PY'
import re
import sys
from pathlib import Path
p = Path(sys.argv[1])
size = sys.argv[2]
text = p.read_text()
text = re.sub(r'ovf:size="[0-9]+"', f'ovf:size="{size}"', text, count=1)
p.write_text(text)
PY

(
  cd "$OUT_DIR"
  sha256sum zenplus-sensor.ovf zenplus-sensor-disk1.vmdk > "$(basename "$MF")"
  tar -cf "$(basename "$OVA")" zenplus-sensor.ovf zenplus-sensor.mf zenplus-sensor-disk1.vmdk
)

chmod 0640 "$OVA" "$OVF" "$MF" "$VMDK"
if id zenplus >/dev/null 2>&1; then
  chown -R zenplus:zenplus "$OUT_DIR"
fi
echo "$OVA"
