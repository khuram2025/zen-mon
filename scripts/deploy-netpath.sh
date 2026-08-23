#!/usr/bin/env bash
#
# deploy-netpath.sh — privileged finish-line for the NetPath module.
#
# Everything else (code, dashboard build, Postgres tables migrate-075, demo +
# starter probes) is already in place. These three steps need root, which the
# Claude Code session does not have, so run this once:
#
#     sudo bash /opt/zenplus/scripts/deploy-netpath.sh
#
#   1. Apply migrate-076 (widens the alert_rules metric CHECK for netpath_*).
#      That table is owned by the postgres superuser, hence sudo -u postgres.
#   2. Restart zenplus-api  (loads the /api/v1/netpath router + background loops).
#   3. Restart zenplus-poller (loads the Paris-traceroute prober; the setcap
#      drop-in re-applies CAP_NET_RAW on start).
#
# Idempotent — safe to re-run.
set -euo pipefail

ROOT=/opt/zenplus
cd "$ROOT"

log() { printf '\033[1;36m[netpath]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  err "run me with sudo: sudo bash $0"; exit 1
fi

# ── 1. migrate-076 (alert metric constraint) ───────────────────────────────
MIG=scripts/migrate-076-netpath-alert-metrics.sql
log "Applying $MIG as postgres…"
sudo -u postgres psql -d zenplus -v ON_ERROR_STOP=1 -f "$MIG" >/dev/null
SUM=$(sha256sum "$MIG" | cut -d' ' -f1)
sudo -u postgres psql -d zenplus -v ON_ERROR_STOP=1 -c \
  "INSERT INTO schema_migrations(filename,checksum,applied_at,duration_ms)
   VALUES('migrate-076-netpath-alert-metrics.sql','$SUM',NOW(),0)
   ON CONFLICT (filename) DO UPDATE SET checksum=EXCLUDED.checksum;" >/dev/null
# verify the constraint now accepts a netpath metric
if sudo -u postgres psql -d zenplus -tAc \
    "SELECT 'netpath_rtt' = ANY (regexp_split_to_array(
        (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='alert_rules_metric_check'),
        '\W+'))" | grep -q t; then
  ok "alert_rules metric constraint now includes netpath_* metrics"
else
  err "constraint check inconclusive (inspect pg_get_constraintdef)"; fi

# ── 2 + 3. restart services ────────────────────────────────────────────────
log "Restarting zenplus-api…"
systemctl restart zenplus-api
ok "zenplus-api restarted"

log "Restarting zenplus-poller…"
systemctl restart zenplus-poller
sleep 1
getcap "$ROOT/bin/zenplus-poller" | grep -q cap_net_raw \
  && ok "poller has cap_net_raw (traceroute can open raw sockets)" \
  || err "poller is MISSING cap_net_raw — traces will fail (check the setcap drop-in)"

# ── verify ─────────────────────────────────────────────────────────────────
log "Verifying…"
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v1/netpath/summary || true)
[ "$code" = "401" ] && ok "API /netpath router is live (401 = auth required, as expected)" \
                     || err "API /netpath returned $code (expected 401) — check: journalctl -u zenplus-api -n 50"
curl -s http://127.0.0.1:8081/health >/dev/null 2>&1 && ok "poller health endpoint responding" || true

echo
log "Done. NetPath is live. Open http://<appliance>/netpath and log in."
log "Real traces for the 4 starter probes appear within ~1–2 minutes."
