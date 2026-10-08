#!/bin/bash
# /opt/zenplus/bin/expand-storage.sh
# Operator CLI for growing the /data volume. Settings -> Storage drives the
# same operations; both go through the root-owned volume helper, which finds
# the logical volume behind /data (dedicated data LV or the root LV) and
# validates every disk before touching it.
#
# Usage:
#   expand-storage.sh /dev/sdb [--wipe]   Add a new disk and give all its space to /data
#   expand-storage.sh --grow              Claim space after enlarging a virtual disk
#   expand-storage.sh --status            Show the current storage layout
#
set -euo pipefail

HELPER="/usr/local/sbin/zenplus-volume-helper"

log() { echo "[storage] $(date +%H:%M:%S) $*"; }
err() { echo "[storage] ERROR: $*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    err "must run as root"
    exit 1
fi
if [ ! -x "$HELPER" ]; then
    err "$HELPER is not installed; run: sudo bash /opt/zenplus/scripts/setup-storage.sh"
    exit 1
fi

# Run a helper verb, print its error message on failure, propagate its status.
helper() {
    local out rc=0
    out="$("$HELPER" "$@")" || rc=$?
    if [ "$rc" -ne 0 ]; then
        err "$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error","failed"))' <<<"$out" 2>/dev/null || echo "$out")"
        return "$rc"
    fi
    printf '%s\n' "$out"
}

# Physical volumes of the volume group that holds /data.
data_pvs() {
    local inv
    inv="$("$HELPER" inventory)"
    python3 - "$inv" <<'PY'
import json, os, sys
inv = json.loads(sys.argv[1])
src = os.path.realpath(((inv.get("mounts") or {}).get("/data") or {}).get("source") or "")
lv = next((l for l in inv.get("lvs", []) if l.get("lv_path") and os.path.realpath(l["lv_path"]) == src), None)
for pv in inv.get("pvs", []):
    if lv and pv.get("vg_name") == lv.get("vg_name") and not pv.get("pv_missing"):
        print(pv["pv_name"])
PY
}

case "${1:-}" in
    --status)
        findmnt --target /data -o TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL
        echo ""
        pvs
        echo ""
        vgs
        echo ""
        lvs
        ;;

    --grow)
        log "Rescanning disks for size changes..."
        helper rescan >/dev/null
        for pv in $(data_pvs); do
            log "Claiming any new space on $pv..."
            helper grow-pv "$pv" >/dev/null || true
        done
        log "Extending /data with all unallocated space..."
        rc=0
        helper extend /data all >/dev/null || rc=$?
        if [ "$rc" -eq 2 ]; then
            err "Nothing to grow. Enlarge the virtual disk or add a new disk first."
            exit 5
        fi
        [ "$rc" -eq 0 ] || exit "$rc"
        df -hT /data
        ;;

    /dev/*)
        log "Adding $1 to the data volume group..."
        if [ "${2:-}" = "--wipe" ]; then
            helper add-disk "$1" --wipe >/dev/null
        else
            helper add-disk "$1" >/dev/null
        fi
        log "Extending /data with the new space..."
        helper extend /data all >/dev/null
        log "Expansion complete."
        df -hT /data
        ;;

    *)
        echo "ZenPlus Storage Expansion Tool"
        echo ""
        echo "Usage:"
        echo "  $0 --status              Show current storage layout"
        echo "  $0 /dev/sdX [--wipe]     Add a new disk and expand /data"
        echo "  $0 --grow                Claim space after enlarging a virtual disk"
        echo ""
        echo "Settings -> Storage -> Disks & Volumes offers the same operations, plus"
        echo "partial allocation and safe disk removal."
        exit 1
        ;;
esac
