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
import sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text())
tmp = Path(sys.argv[2])

name = cfg["sensor_name"]
server_url = cfg["server_url"]
token = cfg["enrollment_token"]
proxy_url = cfg.get("proxy_url") or ""

env_lines = [
    f"ZENPLUS_SERVER_URL={server_url}",
    f"ZENPLUS_ENROLLMENT_TOKEN={token}",
    f"ZENPLUS_SENSOR_NAME={name}",
    "ZENPLUS_VERIFY_TLS=1",
]
if proxy_url:
    env_lines.extend([
        f"HTTP_PROXY={proxy_url}",
        f"HTTPS_PROXY={proxy_url}",
        "NO_PROXY=localhost,127.0.0.1,::1",
    ])
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

console_user = cfg.get("console_username") or "zenadmin"
console_password = cfg.get("console_password") or "Read@123"
if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", console_user):
    raise SystemExit("invalid console username")
(tmp / "console.env").write_text(f"CONSOLE_USER={console_user}\nCONSOLE_PASSWORD={console_password}\n")
PY

WORK_QCOW2="$OUT_DIR/zenplus-sensor-configured.qcow2"
VMDK="$OUT_DIR/zenplus-sensor-disk1.vmdk"
OVF="$OUT_DIR/zenplus-sensor.ovf"
MF="$OUT_DIR/zenplus-sensor.mf"
OVA="$OUT_DIR/zenplus-sensor-configured.ova"

rm -f "$WORK_QCOW2" "$VMDK" "$OVF" "$MF" "$OVA"
cp "$BASE_QCOW2" "$WORK_QCOW2"

source "$TMP/console.env"

export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
virt-customize -a "$WORK_QCOW2" \
  --copy-in "$TMP/sensor.env:/etc/zenplus-sensor" \
  --run-command 'chown root:zenplus-sensor /etc/zenplus-sensor/sensor.env' \
  --run-command 'chmod 0640 /etc/zenplus-sensor/sensor.env' \
  --run-command "id $CONSOLE_USER >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups sudo $CONSOLE_USER" \
  --run-command "printf '%s:%s\n' '$CONSOLE_USER' '$CONSOLE_PASSWORD' | chpasswd" \
  --run-command "install -d -m 0755 /etc/sudoers.d" \
  --run-command "printf '%s ALL=(ALL) NOPASSWD:ALL\n' '$CONSOLE_USER' > /etc/sudoers.d/90-zenplus-console" \
  --run-command "chmod 0440 /etc/sudoers.d/90-zenplus-console" \
  --run-command 'systemctl enable zenplus-sensor.service'

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
