#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APPLIANCE_DIR="$ROOT/sensor-appliance"
OUT_DIR="${ZENPLUS_SENSOR_BUILD_DIR:-$APPLIANCE_DIR/out}"
PACKER_FILE="$APPLIANCE_DIR/packer/zenplus-sensor.pkr.hcl"

command -v packer >/dev/null || {
  echo "packer is required to build the sensor appliance" >&2
  exit 1
}

mkdir -p "$OUT_DIR"

echo "Building zenplus-sensor runtime..."
(
  cd "$ROOT/poller"
  /usr/local/go/bin/go build -o "$OUT_DIR/zenplus-sensor" ./cmd/sensor
)

packer init "$PACKER_FILE"
packer build \
  -var "output_dir=$OUT_DIR" \
  -var "sensor_binary=$OUT_DIR/zenplus-sensor" \
  "$PACKER_FILE"

echo
echo "Build complete. Publish with:"
echo "  sudo $APPLIANCE_DIR/scripts/publish-artifacts.sh --ova $OUT_DIR/zenplus-sensor.ova --ovf $OUT_DIR/zenplus-sensor.ovf"
