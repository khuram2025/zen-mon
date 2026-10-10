#!/bin/bash
# ============================================================================
# Install ZenPlus ClickHouse configuration outside the code tree.
#
# docker-compose.yml bind-mounts these files from /etc/zenplus/clickhouse into
# the ClickHouse container. They live outside /opt/zenplus on purpose: an OTA
# rollback restores and prunes the code tree, and a bind-mount source that
# disappears makes Docker create a directory in its place on the next
# container start, which ClickHouse cannot read. Files here survive rollback,
# so a container created with these mounts always starts again.
#
# Each file is staged next to its destination, validated as a <clickhouse>
# XML document, and renamed into place: an interruption leaves either the old
# or the new complete file, never a partial one. The rename gives the file a
# new inode; a running container keeps reading its (valid) old inode until it
# is re-created, which setup-storage.sh does whenever this prints changed=1.
#
# Idempotent; run as root by install.sh (before ClickHouse first starts) and by
# setup-storage.sh (every OTA).
# ============================================================================
set -euo pipefail

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
DEST="${ZENPLUS_CLICKHOUSE_CONF_DIR:-/etc/zenplus/clickhouse}"
changed=0
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

valid_xml() {
    python3 -I -c 'import sys, xml.etree.ElementTree as E
sys.exit(0 if E.parse(sys.argv[1]).getroot().tag == "clickhouse" else 1)' "$1" 2>/dev/null
}

install_one() {
    local src="$1" dst="$2" dir tmp
    dir="$(dirname "$dst")"
    if [ ! -f "$src" ] || ! valid_xml "$src"; then
        echo "install-clickhouse-config: missing or invalid $src" >&2
        exit 1
    fi
    mkdir -p "$dir"
    chmod 0755 "$dir"
    [ "$IS_ROOT" = 1 ] && chown root:root "$dir"
    # Leftovers of an interrupted earlier run.
    rm -f "$dir"/.zenplus-stage.* 2>/dev/null || true
    # Repair the Docker failure mode: an (empty) directory where the file belongs.
    if [ -d "$dst" ]; then
        rmdir "$dst" 2>/dev/null || { echo "install-clickhouse-config: $dst is a non-empty directory" >&2; exit 1; }
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0
    fi
    tmp="$(mktemp "$dir/.zenplus-stage.XXXXXX")"
    cat "$src" > "$tmp"
    chmod 0644 "$tmp"
    [ "$IS_ROOT" = 1 ] && chown root:root "$tmp"
    if ! valid_xml "$tmp"; then
        rm -f "$tmp"
        echo "install-clickhouse-config: staged copy of $src is invalid" >&2
        exit 1
    fi
    sync -f "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dst"
    changed=1
}

install_one "${ZENPLUS_DIR}/docker/clickhouse/zenplus_logging.xml" "${DEST}/config.d/zenplus_logging.xml"
install_one "${ZENPLUS_DIR}/docker/clickhouse/zenplus_profile.xml" "${DEST}/users.d/zenplus_profile.xml"
echo "changed=${changed}"
