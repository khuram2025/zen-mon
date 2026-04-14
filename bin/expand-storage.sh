#!/bin/bash
# /opt/zenplus/bin/expand-storage.sh
# Expand the /data volume by adding a new disk or growing the existing one.
#
# Usage:
#   expand-storage.sh /dev/sdb          # Add a new disk to the VG
#   expand-storage.sh --grow             # Grow LV after VG was expanded externally
#   expand-storage.sh --status           # Show current storage status
#
set -euo pipefail

VG_NAME="ubuntu-vg"
LV_NAME="data-lv"
MOUNT_POINT="/data"

log() { echo "[storage] $(date +%H:%M:%S) $*"; }
err() { echo "[storage] ERROR: $*" >&2; }

# Pre-flight: ensure LVM can write metadata archives
preflight_check() {
    local archive_dir="/etc/lvm/archive"
    local backup_dir="/etc/lvm/backup"

    # Check root filesystem is writable
    if ! touch /etc/lvm/.write_test 2>/dev/null; then
        err "Root filesystem appears read-only. Attempting remount..."
        mount -o remount,rw / 2>/dev/null || true
        if ! touch /etc/lvm/.write_test 2>/dev/null; then
            err "Cannot write to /etc/lvm — root filesystem is read-only."
            err "Run: mount -o remount,rw /"
            exit 1
        fi
    fi
    rm -f /etc/lvm/.write_test

    # Ensure archive/backup dirs exist and are writable
    mkdir -p "$archive_dir" "$backup_dir" 2>/dev/null || true
    chmod 700 "$archive_dir" "$backup_dir" 2>/dev/null || true
}

case "${1:-}" in
    --status)
        echo "=== Volume Group ==="
        vgs "$VG_NAME"
        echo ""
        echo "=== Logical Volumes ==="
        lvs "$VG_NAME"
        echo ""
        echo "=== /data Filesystem ==="
        df -hT "$MOUNT_POINT"
        echo ""
        echo "=== Data Directory Usage ==="
        du -sh "$MOUNT_POINT"/*/ 2>/dev/null || echo "  (empty)"
        echo ""
        echo "=== Physical Volumes ==="
        pvs
        ;;

    --grow)
        preflight_check
        log "Growing LV $LV_NAME to use all free VG space..."

        # Check if there is actually free space
        FREE_EXTENTS=$(vgs "$VG_NAME" --noheadings --nosuffix -o vg_free_count 2>/dev/null | tr -d ' ')
        if [ "${FREE_EXTENTS:-0}" -eq 0 ]; then
            err "No free extents in $VG_NAME. Add a new disk or resize the VM disk first."
            exit 5
        fi

        lvextend -l +100%FREE "/dev/$VG_NAME/$LV_NAME"
        log "Resizing filesystem..."
        resize2fs "/dev/$VG_NAME/$LV_NAME"
        log "Done."
        df -hT "$MOUNT_POINT"
        ;;

    /dev/*)
        DISK="$1"
        preflight_check

        if [ ! -b "$DISK" ]; then
            err "$DISK is not a valid block device."
            echo "Available disks:"
            lsblk -d -o NAME,SIZE,TYPE,MOUNTPOINTS | grep disk
            exit 1
        fi

        # Check if already a PV in our VG
        if pvs "$DISK" --noheadings 2>/dev/null | grep -q "$VG_NAME"; then
            err "$DISK is already a physical volume in $VG_NAME."
            pvs "$DISK"
            exit 1
        fi

        # If it's a PV but orphaned (not in our VG), just extend the VG
        if pvs "$DISK" &>/dev/null; then
            log "$DISK is already a physical volume (orphaned). Adding to $VG_NAME..."
        else
            log "Creating physical volume on $DISK..."
            pvcreate -f "$DISK"
        fi

        log "Extending volume group $VG_NAME with $DISK..."
        vgextend "$VG_NAME" "$DISK"

        log "Extending logical volume $LV_NAME to use all free space..."
        lvextend -l +100%FREE "/dev/$VG_NAME/$LV_NAME"

        log "Resizing ext4 filesystem (online)..."
        resize2fs "/dev/$VG_NAME/$LV_NAME"

        log "Expansion complete."
        echo ""
        echo "=== New Storage Layout ==="
        pvs
        echo ""
        vgs "$VG_NAME"
        echo ""
        df -hT "$MOUNT_POINT"
        ;;

    *)
        echo "ZenPlus Storage Expansion Tool"
        echo ""
        echo "Usage:"
        echo "  $0 --status              Show current storage layout"
        echo "  $0 /dev/sdX              Add a new disk and expand /data"
        echo "  $0 --grow                Grow /data after external VG expansion"
        echo ""
        echo "Examples:"
        echo "  # Add a new 500GB disk:"
        echo "  sudo $0 /dev/sdb"
        echo ""
        echo "  # After expanding a VMware disk:"
        echo "  sudo pvresize /dev/sda3    # resize existing PV to see new space"
        echo "  sudo $0 --grow             # extend LV + filesystem"
        echo ""
        echo "  # Add multiple disks:"
        echo "  sudo $0 /dev/sdb"
        echo "  sudo $0 /dev/sdc"
        exit 1
        ;;
esac
