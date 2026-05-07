#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APPLIANCE_DIR="$ROOT/sensor-appliance"
OUT_DIR="${ZENPLUS_SENSOR_OUT_DIR:-$APPLIANCE_DIR/out/real-ova}"
BASE_DIR="${ZENPLUS_SENSOR_BASE_DIR:-$APPLIANCE_DIR/out/base}"
BASE_URL="${ZENPLUS_SENSOR_BASE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
BASE_IMG="$BASE_DIR/$(basename "$BASE_URL")"
WORK_QCOW2="$OUT_DIR/zenplus-sensor.qcow2"
VMDK="$OUT_DIR/zenplus-sensor-disk1.vmdk"
OVF="$OUT_DIR/zenplus-sensor.ovf"
MF="$OUT_DIR/zenplus-sensor.mf"
OVA="$OUT_DIR/zenplus-sensor.ova"
METADATA="$OUT_DIR/BUILD-METADATA.json"
SENSOR_BIN="$OUT_DIR/zenplus-sensor"
DISK_GB="${ZENPLUS_SENSOR_DISK_GB:-12}"
MEMORY_MB="${ZENPLUS_SENSOR_MEMORY_MB:-1024}"
VCPU="${ZENPLUS_SENSOR_VCPU:-1}"
VERSION="${ZENPLUS_SENSOR_VERSION:-0.1.0}"
DEFAULT_CONSOLE_USER="${ZENPLUS_SENSOR_DEFAULT_CONSOLE_USER:-zenadmin}"
DEFAULT_CONSOLE_PASSWORD="${ZENPLUS_SENSOR_DEFAULT_CONSOLE_PASSWORD:-Read@123}"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

require curl
require qemu-img
require virt-customize
require sha256sum
require tar

mkdir -p "$OUT_DIR" "$BASE_DIR"

if [[ ! -f "$BASE_IMG" ]]; then
  echo "Downloading Ubuntu cloud image: $BASE_URL"
  curl -L --fail --continue-at - --output "$BASE_IMG" "$BASE_URL"
fi

echo "Building zenplus-sensor runtime"
(
  cd "$ROOT/poller"
  /usr/local/go/bin/go build -trimpath -ldflags="-s -w" -o "$SENSOR_BIN" ./cmd/sensor
)

rm -f "$WORK_QCOW2" "$VMDK" "$OVF" "$MF" "$OVA" "$METADATA"
qemu-img convert -O qcow2 "$BASE_IMG" "$WORK_QCOW2"
qemu-img resize "$WORK_QCOW2" "${DISK_GB}G"

export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
virt-customize -a "$WORK_QCOW2" \
  --copy-in "$SENSOR_BIN:/usr/local/bin" \
  --copy-in "$APPLIANCE_DIR/systemd/zenplus-sensor.service:/etc/systemd/system" \
  --mkdir /etc/zenplus-sensor \
  --mkdir /var/lib/zenplus-sensor \
  --mkdir /var/log/zenplus-sensor \
  --copy-in "$APPLIANCE_DIR/config/zenplus-sensor.env.example:/etc/zenplus-sensor" \
  --run-command 'groupadd --system zenplus-sensor || true' \
  --run-command 'id zenplus-sensor >/dev/null 2>&1 || useradd --system --home /var/lib/zenplus-sensor --shell /usr/sbin/nologin --gid zenplus-sensor zenplus-sensor' \
  --run-command 'chmod 0755 /usr/local/bin/zenplus-sensor' \
  --run-command 'chmod 0750 /etc/zenplus-sensor /var/lib/zenplus-sensor /var/log/zenplus-sensor' \
  --run-command 'chown root:root /etc/zenplus-sensor' \
  --run-command 'chown -R zenplus-sensor:zenplus-sensor /var/lib/zenplus-sensor /var/log/zenplus-sensor' \
  --run-command 'mv /etc/zenplus-sensor/zenplus-sensor.env.example /etc/zenplus-sensor/sensor.env.example' \
  --run-command 'chown root:zenplus-sensor /etc/zenplus-sensor/sensor.env.example' \
  --run-command 'chmod 0640 /etc/zenplus-sensor/sensor.env.example' \
  --run-command 'setcap cap_net_raw+ep /usr/local/bin/zenplus-sensor || true' \
  --run-command "id $DEFAULT_CONSOLE_USER >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups sudo $DEFAULT_CONSOLE_USER" \
  --run-command "printf '%s:%s\n' '$DEFAULT_CONSOLE_USER' '$DEFAULT_CONSOLE_PASSWORD' | chpasswd" \
  --run-command "install -d -m 0755 /etc/sudoers.d" \
  --run-command "printf '%s ALL=(ALL) NOPASSWD:ALL\n' '$DEFAULT_CONSOLE_USER' > /etc/sudoers.d/90-zenplus-console" \
  --run-command "chmod 0440 /etc/sudoers.d/90-zenplus-console" \
  --run-command 'systemctl enable zenplus-sensor.service' \
  --run-command 'systemctl enable cloud-init.service cloud-config.service cloud-final.service || true' \
  --run-command 'cloud-init clean --logs || true'

echo "Converting disk to stream-optimized VMDK"
qemu-img convert -O vmdk -o subformat=streamOptimized "$WORK_QCOW2" "$VMDK"

VMDK_SIZE="$(stat -c%s "$VMDK")"
CAPACITY_BYTES="$((DISK_GB * 1024 * 1024 * 1024))"
cat > "$OVF" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
          xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
          xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:id="file1" ovf:href="zenplus-sensor-disk1.vmdk" ovf:size="$VMDK_SIZE"/>
  </References>
  <DiskSection>
    <Info>Virtual disk information</Info>
    <Disk ovf:diskId="vmdisk1" ovf:fileRef="file1" ovf:capacity="$CAPACITY_BYTES" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>
  <NetworkSection>
    <Info>Logical networks</Info>
    <Network ovf:name="VM Network">
      <Description>Default bridged or port-group network</Description>
    </Network>
  </NetworkSection>
  <VirtualSystem ovf:id="zenplus-sensor">
    <Info>ZenPlus Remote Sensor Appliance</Info>
    <Name>ZenPlus Remote Sensor</Name>
    <OperatingSystemSection ovf:id="94" vmw:osType="ubuntu64Guest">
      <Info>Ubuntu 24.04 LTS 64-bit</Info>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware requirements</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>zenplus-sensor</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>vmx-17</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
        <rasd:Description>Number of virtual CPUs</rasd:Description>
        <rasd:ElementName>${VCPU} virtual CPU(s)</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>$VCPU</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:Description>Memory size</rasd:Description>
        <rasd:ElementName>${MEMORY_MB}MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>$MEMORY_MB</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Description>SCSI Controller</rasd:Description>
        <rasd:ElementName>SCSI Controller 0</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>lsilogic</rasd:ResourceSubType>
        <rasd:ResourceType>6</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:Description>Hard disk</rasd:Description>
        <rasd:ElementName>Hard disk 1</rasd:ElementName>
        <rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Connection>VM Network</rasd:Connection>
        <rasd:Description>Ethernet adapter</rasd:Description>
        <rasd:ElementName>Network adapter 1</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
EOF

(
  cd "$OUT_DIR"
  sha256sum zenplus-sensor.ovf zenplus-sensor-disk1.vmdk > "$(basename "$MF")"
  tar -cf "$(basename "$OVA")" zenplus-sensor.ovf zenplus-sensor.mf zenplus-sensor-disk1.vmdk
)

cat > "$METADATA" <<EOF
{
  "type": "bootable-ova",
  "version": "$VERSION",
  "base": "ubuntu-24.04-noble-cloudimg-amd64",
  "disk_gb": $DISK_GB,
  "memory_mb": $MEMORY_MB,
  "vcpu": $VCPU,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo "Built real bootable sensor appliance:"
ls -lh "$OVA" "$OVF" "$VMDK" "$METADATA"
