#!/usr/bin/env bash
set -euo pipefail

DEST="${ZENPLUS_SENSOR_ARTIFACT_DIR:-/opt/zenplus/artifacts/sensors}"
OVA=""
OVF=""
METADATA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ova) OVA="${2:-}"; shift 2 ;;
    --ovf) OVF="${2:-}"; shift 2 ;;
    --metadata) METADATA="${2:-}"; shift 2 ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$OVA" && -z "$OVF" ]]; then
  echo "usage: $0 --ova /path/zenplus-sensor.ova --ovf /path/zenplus-sensor.ovf [--metadata /path/BUILD-METADATA.json] [--dest /opt/zenplus/artifacts/sensors]" >&2
  exit 2
fi

install -d -m 0755 "$DEST"
if id zenplus >/dev/null 2>&1; then
  install -d -m 0750 -o zenplus -g zenplus "$DEST/bootstrap"
else
  install -d -m 0750 "$DEST/bootstrap"
fi

if [[ -n "$OVA" ]]; then
  install -m 0644 "$OVA" "$DEST/zenplus-sensor.ova"
fi
if [[ -n "$OVF" ]]; then
  install -m 0644 "$OVF" "$DEST/zenplus-sensor.ovf"
fi
if [[ -n "$METADATA" ]]; then
  install -m 0644 "$METADATA" "$DEST/BUILD-METADATA.json"
fi

(
  cd "$DEST"
  rm -f SHA256SUMS
  for f in zenplus-sensor.ova zenplus-sensor.ovf BUILD-METADATA.json; do
    [[ -f "$f" ]] && sha256sum "$f" >> SHA256SUMS
  done
)

echo "Published sensor artifacts to $DEST"
ls -lh "$DEST"
