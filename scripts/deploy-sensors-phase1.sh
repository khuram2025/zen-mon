#!/usr/bin/env bash
# Deploy the Phase 1 remote-sensors feature to a running ZenPlus install.
#
# Idempotent. Safe to re-run. Run from the repo root with sudo:
#
#   sudo bash scripts/deploy-sensors-phase1.sh
#
# What it does:
#   1. Applies migrate-008-sensors.sql to the live Postgres database.
#   2. Copies new/modified server files into /opt/zenplus/server/.
#   3. Copies mock_sensor.py into /opt/zenplus/scripts/.
#   4. Builds the dashboard (vite build only — skips tsc due to pre-existing errors).
#   5. Copies dist/ into /opt/zenplus/dashboard/dist/.
#   6. Restarts zenplus-api.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/zenplus"
DB_NAME="${ZENPLUS_DB:-zenplus}"

# ── sanity ────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi
if [ ! -d "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR not found — is ZenPlus installed?" >&2
    exit 1
fi
echo "→ repo:    $REPO_DIR"
echo "→ install: $INSTALL_DIR"
echo

# ── 1. migrate ────────────────────────────────────────────────────────
echo "[1/6] Applying database migration…"
# Copy to /tmp first because /home/zen is not readable by postgres user.
TMP_SQL="$(mktemp /tmp/zp-migrate-008.XXXXXX.sql)"
cp "$REPO_DIR/scripts/migrate-008-sensors.sql" "$TMP_SQL"
chmod 0644 "$TMP_SQL"
su - postgres -c "psql -d '$DB_NAME' -f '$TMP_SQL'" >/dev/null
su - postgres -c "psql -d '$DB_NAME' -c 'GRANT ALL ON sensors, sites, sensor_assignments TO zenplus;'" >/dev/null
rm -f "$TMP_SQL"
echo "    ok"

# ── 2. copy server files ──────────────────────────────────────────────
echo "[2/6] Copying server files…"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/main.py" \
    "$INSTALL_DIR/server/app/main.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/models/__init__.py" \
    "$INSTALL_DIR/server/app/models/__init__.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/models/sensor.py" \
    "$INSTALL_DIR/server/app/models/sensor.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/schemas/sensor.py" \
    "$INSTALL_DIR/server/app/schemas/sensor.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/api/v1/sensors.py" \
    "$INSTALL_DIR/server/app/api/v1/sensors.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/server/app/api/v1/sensor_api.py" \
    "$INSTALL_DIR/server/app/api/v1/sensor_api.py"
echo "    ok"

# ── 3. copy mock sensor + migration to scripts/ ───────────────────────
echo "[3/6] Copying scripts…"
mkdir -p "$INSTALL_DIR/scripts"
install -m 0755 -o zenplus -g zenplus \
    "$REPO_DIR/scripts/mock_sensor.py" \
    "$INSTALL_DIR/scripts/mock_sensor.py"
install -m 0644 -o zenplus -g zenplus \
    "$REPO_DIR/scripts/migrate-008-sensors.sql" \
    "$INSTALL_DIR/scripts/migrate-008-sensors.sql"
echo "    ok"

# ── 4. build dashboard ────────────────────────────────────────────────
echo "[4/6] Building dashboard (vite build)…"
cd "$REPO_DIR/dashboard"
if [ ! -d node_modules ]; then
    sudo -u zen npm ci --silent 2>/dev/null || sudo -u zen npm install --silent
fi
sudo -u zen npx vite build > /tmp/vite-build.log 2>&1 || {
    echo "    FAIL — see /tmp/vite-build.log"
    tail -20 /tmp/vite-build.log
    exit 1
}
echo "    ok ($(grep -oE '[0-9]+ modules transformed' /tmp/vite-build.log || echo 'built'))"

# ── 5. deploy dashboard ───────────────────────────────────────────────
echo "[5/6] Deploying dashboard dist/…"
# nginx serves from /var/www/zenplus; we also keep a copy in the install
# dir for reference but nginx never reads it.
rm -rf "$INSTALL_DIR/dashboard/dist"
mkdir -p "$INSTALL_DIR/dashboard/dist"
cp -a "$REPO_DIR/dashboard/dist/." "$INSTALL_DIR/dashboard/dist/"
chown -R zenplus:zenplus "$INSTALL_DIR/dashboard/dist"

WEBROOT=/var/www/zenplus
mkdir -p "$WEBROOT"
rm -rf "$WEBROOT/assets"
cp -a "$REPO_DIR/dashboard/dist/." "$WEBROOT/"
# Web root is owned by 'zen' on this appliance; preserve that so future
# rebuilds without sudo also work.
chown -R zen:zen "$WEBROOT" 2>/dev/null || true
echo "    ok"

# ── 6. restart api ────────────────────────────────────────────────────
echo "[6/6] Restarting zenplus-api…"
systemctl restart zenplus-api
sleep 2
if systemctl is-active --quiet zenplus-api; then
    echo "    ok — service is active"
else
    echo "    FAIL — service did not come up; tail of journal:"
    journalctl -u zenplus-api -n 30 --no-pager
    exit 1
fi

echo
echo "Done. Open http://$(hostname -I | awk '{print $1}')/settings/general"
echo "Scroll to the 'Remote Sensors' card and click 'Add Sensor'."
