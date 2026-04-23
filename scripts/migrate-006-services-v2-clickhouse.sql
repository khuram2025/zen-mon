-- Migration 006 — Services feature v2 (ClickHouse side)
-- Matches scripts/migrate-006-services-v2.sql on the Postgres side. Safe to re-run.
-- Apply with:  docker exec -i zenplus-clickhouse clickhouse-client --password $CH_PW < migrate-006-services-v2-clickhouse.sql

-- ─── service_metrics (raw, 30d) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.service_metrics (
    service_check_id UUID,
    device_id        Nullable(UUID),
    timestamp        DateTime64(3, 'UTC'),
    check_type       String,
    is_up            UInt8,
    response_ms      Float64,
    status_code        Nullable(UInt16),
    tls_days_remaining Nullable(Int32),
    tls_valid          Nullable(UInt8),
    content_matched    Nullable(UInt8),
    error_message      Nullable(String),
    poller_id          String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- Make device_id nullable if a previous migration created it NOT NULL.
ALTER TABLE zenplus.service_metrics MODIFY COLUMN device_id Nullable(UUID);

-- ─── service_metrics_5m (rollup, 90d) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.service_metrics_5m (
    service_check_id UUID,
    device_id        Nullable(UUID),
    timestamp        DateTime64(3, 'UTC'),
    check_type       LowCardinality(String),
    avg_response_ms  Float64,
    min_response_ms  Float64,
    max_response_ms  Float64,
    uptime_pct       Float32,
    sample_count     UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

-- ─── Materialised view: service_metrics → 5m rollup ────────────────────────
-- Note: alias the bucketed time as `timestamp` (not `ts`) to match the target column.
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.service_metrics_5m_mv
TO zenplus.service_metrics_5m
AS SELECT
    service_check_id,
    any(device_id)                    AS device_id,
    toStartOfFiveMinutes(timestamp)   AS timestamp,
    any(check_type)                   AS check_type,
    avg(response_ms)                  AS avg_response_ms,
    min(response_ms)                  AS min_response_ms,
    max(response_ms)                  AS max_response_ms,
    avg(is_up)                        AS uptime_pct,
    count()                           AS sample_count
FROM zenplus.service_metrics
GROUP BY service_check_id, timestamp;

-- ─── service_status_log (1y) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.service_status_log (
    service_check_id UUID,
    device_id        Nullable(UUID),
    timestamp        DateTime64(3, 'UTC'),
    check_type       String,
    old_status       String,
    new_status       String,
    reason           String,
    duration_sec     UInt64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

ALTER TABLE zenplus.service_status_log MODIFY COLUMN device_id Nullable(UUID);
