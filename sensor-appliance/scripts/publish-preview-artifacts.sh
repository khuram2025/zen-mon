#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${ZENPLUS_SENSOR_ARTIFACT_DIR:-/opt/zenplus/artifacts/sensors}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

GO_BIN="${GO_BIN:-/usr/local/go/bin/go}"
if [[ ! -x "$GO_BIN" ]]; then
  echo "Go tool not found at $GO_BIN" >&2
  exit 1
fi

echo "Building zenplus-sensor runtime..."
(
  cd "$ROOT/poller"
  "$GO_BIN" build -o "$WORK/zenplus-sensor" ./cmd/sensor
)

mkdir -p "$WORK/zenplus-sensor-preview"
install -m 0755 "$WORK/zenplus-sensor" "$WORK/zenplus-sensor-preview/zenplus-sensor"
cp -R "$ROOT/sensor-appliance/config" "$WORK/zenplus-sensor-preview/"
cp -R "$ROOT/sensor-appliance/scripts" "$WORK/zenplus-sensor-preview/"
cp -R "$ROOT/sensor-appliance/systemd" "$WORK/zenplus-sensor-preview/"
cat > "$WORK/zenplus-sensor-preview/README.txt" <<'TXT'
ZenPlus Remote Sensor developer-preview artifact.

This archive is published so the controller download and onboarding flow can be
tested immediately. It contains the compiled zenplus-sensor runtime, systemd
unit, env template, and install scripts.

This is NOT a bootable virtual appliance yet. A deployable OVA/OVF requires
the Packer/QEMU appliance image pipeline.
TXT

cat > "$WORK/zenplus-sensor.ovf" <<'OVF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- ZenPlus Remote Sensor developer-preview OVF descriptor.
     This descriptor is a placeholder for controller download/onboarding tests.
     It is not a deployable VM until the Packer/QEMU image build publishes a disk. -->
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1">
  <References/>
  <DiskSection>
    <Info>No virtual disk is included in this developer-preview descriptor.</Info>
  </DiskSection>
  <VirtualSystem ovf:id="zenplus-sensor-preview" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">
    <Info>ZenPlus Remote Sensor developer preview</Info>
    <Name>zenplus-sensor-preview</Name>
  </VirtualSystem>
</Envelope>
OVF

tar -C "$WORK" -cf "$WORK/zenplus-sensor.ova" zenplus-sensor.ovf zenplus-sensor-preview

install -d -m 0755 "$DEST"
install -m 0644 "$WORK/zenplus-sensor.ova" "$DEST/zenplus-sensor.ova"
install -m 0644 "$WORK/zenplus-sensor.ovf" "$DEST/zenplus-sensor.ovf"

(
  cd "$DEST"
  sha256sum zenplus-sensor.ova zenplus-sensor.ovf > SHA256SUMS
)

echo "Published preview sensor artifacts:"
ls -lh "$DEST"
echo
cat "$DEST/SHA256SUMS"
