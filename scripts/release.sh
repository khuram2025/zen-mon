#!/bin/bash
# ============================================================================
# ZenPlus release: build + publish an OTA update to zentryc.com
#
#   sudo bash scripts/release.sh <version> "<changelog>" [severity] [rollout]
#
#   severity: normal (default) | security | critical | optional
#   rollout:  full (default)   | canary   | percentage
#
# Handles every gotcha that has bitten past releases:
#   - dashboard/dist and /tmp/zenplus-releases must be zenplus-owned before the
#     build (vite emptyDir's dist in place; a stray zen-owned dist kills it),
#     and world-readable afterwards so nginx can serve it
#   - build must run as zenplus: the signing key updater/keys/zentryc-release.key
#     is 0400 zenplus
#   - publish needs httpx, which is only in server/venv, not system python
#   - get_admin_token() reads $HOME/.zenplus-admin-creds, and sudo keeps the
#     caller's HOME, so HOME=/opt/zenplus must be passed explicitly
#
# Credentials live in /root/.zenplus-admin-creds (0600 root) and are copied to
# the zenplus HOME only for the duration of the publish, then removed.
# Account: admin@zentryc.com. (The older zenai-release@zentryc.com account no
# longer authenticates — a 401 at publish time usually means a stale file here.)
#
# Pre-flight, run these BEFORE calling this script:
#   1. bump .version  (2 lines: version, ISO timestamp — nothing writes it)
#   2. server/venv/bin/python -m pytest server/tests -q
#   3. python3 scripts/build-release.py lint-migrations
#      (new migrate-*.sql also needs: lint-migrations --update-lock)
#   4. commit + push — releases are cut from the active feature branch
# ============================================================================
set -euo pipefail
cd /opt/zenplus

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must run as root (sudo bash scripts/release.sh ...)" >&2
    exit 1
fi
if [ $# -lt 2 ]; then
    echo "usage: sudo bash scripts/release.sh <version> \"<changelog>\" [severity] [rollout]" >&2
    exit 1
fi

VERSION="$1"
CHANGELOG="$2"
SEVERITY="${3:-normal}"
ROLLOUT="${4:-full}"
CREDS_SRC="/root/.zenplus-admin-creds"
CREDS_DST="/opt/zenplus/.zenplus-admin-creds"
ZUP="/tmp/zenplus-releases/update-${VERSION}.zup"

[ -f "$CREDS_SRC" ] || { echo "Missing $CREDS_SRC — cannot authenticate to zentryc.com" >&2; exit 1; }

echo "== Fixing ownership (vite builds dist in place as zenplus) =="
mkdir -p /tmp/zenplus-releases
chown -R zenplus:zenplus /opt/zenplus/dashboard/dist /tmp/zenplus-releases 2>/dev/null || true
chown -R zenplus:zenplus /opt/zenplus/.git/objects 2>/dev/null || true

install -o zenplus -g zenplus -m 0600 "$CREDS_SRC" "$CREDS_DST"
cleanup() {
    rm -f "$CREDS_DST"
    chmod -R a+rX /opt/zenplus/dashboard/dist 2>/dev/null || true
}
trap cleanup EXIT

echo "== Building ${VERSION} =="
sudo -u zenplus env HOME=/opt/zenplus python3 scripts/build-release.py build \
    --version "$VERSION" --changelog "$CHANGELOG" --severity "$SEVERITY"

echo "== Verifying manifest signature before publishing =="
sudo -u zenplus env HOME=/opt/zenplus server/venv/bin/python - "$ZUP" <<'PY'
import sys, tarfile, tempfile, os
sys.path.insert(0, "/opt/zenplus")
from updater.crypto import verify_manifest
with tempfile.TemporaryDirectory() as d:
    with tarfile.open(sys.argv[1], "r:gz") as t:
        t.extract("manifest.json", d); t.extract("manifest.json.sig", d)
    m = verify_manifest(os.path.join(d, "manifest.json"),
                        os.path.join(d, "manifest.json.sig"),
                        "/opt/zenplus/updater/keys/zentryc-release.pub")
print(f"  Signature valid — v{m['version']} ({m['severity']}), {len(m['steps'])} steps")
PY

echo "== Publishing ${VERSION} (rollout: ${ROLLOUT}) =="
sudo -u zenplus env HOME=/opt/zenplus server/venv/bin/python scripts/build-release.py publish \
    --file "$ZUP" --version "$VERSION" --changelog "$CHANGELOG" \
    --severity "$SEVERITY" --rollout "$ROLLOUT"
