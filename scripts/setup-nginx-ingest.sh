#!/bin/bash
# Reconcile the active nginx site with the bounded host-results proxy route.
#
# This hook is intentionally narrow: it preserves the appliance's existing
# HTTP/HTTPS and certificate configuration and only inserts (or refreshes) the
# exact host-results location ahead of the generic /api/ location. It is safe
# to run on every OTA release.
set -euo pipefail

GREEN='\033[0;32m'
NC='\033[0m'
log() { echo -e "${GREEN}[setup-nginx-ingest]${NC} $1"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must run as root (sudo bash scripts/setup-nginx-ingest.sh)" >&2
    exit 1
fi

if [ -e /etc/nginx/sites-enabled/zenplus ]; then
    target=/etc/nginx/sites-available/zenplus
else
    target=/etc/nginx/conf.d/zenplus.conf
fi

if [ ! -f "$target" ]; then
    echo "ZenPlus nginx configuration not found at $target" >&2
    exit 1
fi

backup="${target}.zpingest.bak"
cp -a "$target" "$backup"

reconciler="${ZENPLUS_DIR:-/opt/zenplus}/scripts/reconcile-nginx-ingest.py"
if [ ! -f "$reconciler" ]; then
    echo "Nginx ingest reconciler not found at $reconciler" >&2
    exit 1
fi
result=$(python3 "$reconciler" "$target")

rollback() {
    mv "$backup" "$target"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
}

if ! nginx -t >/dev/null 2>&1; then
    nginx -t 2>&1 | tail -5 >&2 || true
    rollback
    echo "nginx validation failed; previous configuration restored" >&2
    exit 1
fi

if [ "$result" = "changed" ]; then
    if ! systemctl reload nginx; then
        rollback
        echo "nginx reload failed; previous configuration restored" >&2
        exit 1
    fi
    log "Installed bounded host-results route in $target"
else
    log "Bounded host-results route already current in $target"
fi

rm -f "$backup"
log "Nginx host-results ingest setup complete"
