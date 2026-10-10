#!/bin/bash
# ============================================================================
# OTA rollback step: reconcile host state that setup-storage.sh changed.
#
# Runs from the signed release payload (like enforce-support-security-floor.sh)
# after ota-restore-backup.py has put the previous code back, so it also runs
# when an older updater (1.23.9+) performs the rollback. Best effort: every
# action is logged; the exit status reports problems without stopping the
# remaining rollback steps.
#
# 1. Systemd units installed by this package (NetFlow collector, NCM, syslog,
#    storage watchdog): back to their pre-update state (ota-snapshot-units.py),
#    so start_services never starts a unit whose binary or module the restored
#    version lacks. A stale ingest-pause marker goes with a removed watchdog.
# 2. ClickHouse: a container whose bind-mount source no longer exists will not
#    start again (Docker creates a directory in its place). If any is missing,
#    recreate the container from the restored docker-compose.yml.
#
# Deliberately left in place (harmless or protective for older versions):
# /etc/zenplus/clickhouse (bounded logging keeps working until the container
# is recreated), journald and logrotate caps, root storage helpers and their
# sudoers entries (they validate their own arguments), ClickHouse tables
# renamed by a configuration change (no monitoring data is deleted).
# ============================================================================
set -u

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
CH_CONTAINER="zenplus-clickhouse"
UNIT="${ZENPLUS_SYSTEMD_DIR:-/etc/systemd/system}/zenplus-storage-watchdog.service"
STATE_DIR="${ZENPLUS_STORAGE_STATE_DIR:-/var/lib/zenplus-storage}"
rc=0
log() { echo "[storage-rollback] $*"; }

# ── 1. Systemd units ────────────────────────────────────────────────────────
# Units this package installed (install_systemd steps, the storage watchdog)
# go back to their pre-update state from the snapshot ota-snapshot-units.py
# took right after the backup: restored if they existed, otherwise disabled,
# stopped and removed (e.g. a 1.23.9 appliance never had the NetFlow collector
# or NCM worker). Without a snapshot, units that cannot run on the restored
# version are removed.
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 -I "$HERE/ota-snapshot-units.py" --restore || rc=1
if [ -f "$UNIT" ]; then
    # Run the restored code, not the newer module still loaded in memory.
    systemctl try-restart zenplus-storage-watchdog.service || rc=1
else
    rm -f "${STATE_DIR}/ingest-paused"
fi

# ── 2. ClickHouse bind mounts ───────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker inspect "$CH_CONTAINER" >/dev/null 2>&1; then
    missing=""
    while IFS= read -r src; do
        [ -z "$src" ] && continue
        if [ ! -e "$src" ]; then
            missing="${missing} ${src}"
        elif [ -d "$src" ] && [[ "$src" == *.xml ]]; then
            # A directory Docker created where a configuration file belongs.
            missing="${missing} ${src}(directory)"
        fi
    done < <(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{println}}{{end}}{{end}}' "$CH_CONTAINER" 2>/dev/null)

    if [ -n "$missing" ]; then
        log "ClickHouse bind-mount sources missing:${missing}; recreating from the restored docker-compose.yml"
        for d in $missing; do
            d="${d%(directory)}"
            [ -d "$d" ] && [[ "$d" == *.xml ]] && rmdir "$d" 2>/dev/null
        done
        if (cd "$ZENPLUS_DIR" && docker compose up -d --force-recreate clickhouse >/dev/null 2>&1); then
            health=""
            for _ in $(seq 1 36); do
                health="$(docker inspect -f '{{.State.Health.Status}}' "$CH_CONTAINER" 2>/dev/null || true)"
                [ "$health" = "healthy" ] && break
                sleep 5
            done
            if [ "$health" = "healthy" ]; then
                log "ClickHouse recreated (healthy)"
            else
                log "ClickHouse recreated but not healthy (status: ${health:-unknown})"
                rc=1
            fi
        else
            log "docker compose could not recreate ClickHouse"
            rc=1
        fi
    else
        log "ClickHouse bind-mount sources present; container left as is"
    fi
fi

exit "$rc"
