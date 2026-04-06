#!/usr/bin/env bash
set -euo pipefail
MAX_WAIT=90; SLEEP_SEC=2
log() { echo "[zenplus-deps] $*"; }
log "Waiting for PostgreSQL..."
elapsed=0
until pg_isready -h localhost -p 5432 -U zenplus -q 2>/dev/null; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: PostgreSQL not ready"; exit 1; }
done
log "PostgreSQL ready (${elapsed}s)"
log "Waiting for Redis..."
elapsed=0
until redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: Redis not ready"; exit 1; }
done
log "Redis ready (${elapsed}s)"
log "Waiting for ClickHouse..."
elapsed=0
until docker exec zenplus-clickhouse clickhouse-client --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1" >/dev/null 2>&1; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: ClickHouse not ready"; exit 1; }
done
log "ClickHouse ready (${elapsed}s)"
log "All dependencies ready"
