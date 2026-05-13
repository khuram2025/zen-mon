-- Migration 004 (ClickHouse): SNMP metrics, interface counters, and traps
-- Conventions match init-clickhouse.sql:
--   database = zenplus
--   timestamp column name = `timestamp`, type DateTime64(3, 'UTC')
--   MergeTree + monthly partition + TTL
-- Safe to re-run.

-- ─── Raw scalar SNMP metrics (30-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics (
    device_id   UUID,
    metric_key  LowCardinality(String),
    value       Float64,
    unit        LowCardinality(String),
    timestamp   DateTime64(3, 'UTC'),
    poller_id   String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, metric_key, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- ─── 5-minute rollup (90-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics_5m (
    device_id    UUID,
    metric_key   LowCardinality(String),
    timestamp    DateTime64(3, 'UTC'),
    avg_value    Float64,
    min_value    Float64,
    max_value    Float64,
    sample_count UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, metric_key, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.snmp_metrics_5m_mv
TO zenplus.snmp_metrics_5m
AS SELECT
    device_id,
    metric_key,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(value)   AS avg_value,
    min(value)   AS min_value,
    max(value)   AS max_value,
    count()      AS sample_count
FROM zenplus.snmp_metrics
GROUP BY device_id, metric_key, timestamp;

-- ─── 1-hour rollup (1-year retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics_1h (
    device_id    UUID,
    metric_key   LowCardinality(String),
    timestamp    DateTime64(3, 'UTC'),
    avg_value    Float64,
    min_value    Float64,
    max_value    Float64,
    p95_value    Float64,
    sample_count UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, metric_key, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

-- NOTE: 5m→1h materialized view deferred. ClickHouse 24.x rejects
-- avg(avg_value) AS avg_value because the alias shadows the source
-- column inside the aggregate. The 1h table is created above and can
-- be backfilled on-demand via a scheduled INSERT … SELECT, or via an
-- MV that uses non-conflicting aliases (revisit in Phase 1).

-- ─── Raw interface counters (30-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_if_metrics (
    device_id      UUID,
    if_index       UInt32,
    timestamp      DateTime64(3, 'UTC'),
    in_octets      UInt64,
    out_octets     UInt64,
    in_errors      UInt64,
    out_errors     UInt64,
    in_discards    UInt64,
    out_discards   UInt64,
    in_ucast_pkts  UInt64,
    out_ucast_pkts UInt64,
    oper_status    UInt8,
    in_bps         Float64,
    out_bps        Float64,
    poller_id      String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, if_index, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- ─── Interface 5-minute rollup (90-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_if_metrics_5m (
    device_id      UUID,
    if_index       UInt32,
    timestamp      DateTime64(3, 'UTC'),
    avg_in_bps     Float64,
    avg_out_bps    Float64,
    max_in_bps     Float64,
    max_out_bps    Float64,
    sum_in_errors  UInt64,
    sum_out_errors UInt64,
    sum_in_discards  UInt64,
    sum_out_discards UInt64,
    sample_count   UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, if_index, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.snmp_if_metrics_5m_mv
TO zenplus.snmp_if_metrics_5m
AS SELECT
    device_id,
    if_index,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(in_bps)         AS avg_in_bps,
    avg(out_bps)        AS avg_out_bps,
    max(in_bps)         AS max_in_bps,
    max(out_bps)        AS max_out_bps,
    sum(in_errors)      AS sum_in_errors,
    sum(out_errors)     AS sum_out_errors,
    sum(in_discards)    AS sum_in_discards,
    sum(out_discards)   AS sum_out_discards,
    count()             AS sample_count
FROM zenplus.snmp_if_metrics
GROUP BY device_id, if_index, timestamp;

-- ─── Interface 1-hour rollup (1-year retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_if_metrics_1h (
    device_id      UUID,
    if_index       UInt32,
    timestamp      DateTime64(3, 'UTC'),
    avg_in_bps     Float64,
    avg_out_bps    Float64,
    p95_in_bps     Float64,
    p95_out_bps    Float64,
    max_in_bps     Float64,
    max_out_bps    Float64,
    sum_in_errors  UInt64,
    sum_out_errors UInt64,
    sample_count   UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, if_index, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

-- NOTE: interface 5m→1h MV deferred — same alias shadowing issue as above.

-- ─── SNMP traps (30-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.snmp_traps (
    device_id   Nullable(UUID),
    source_ip   IPv4,
    trap_oid    String,
    trap_name   String,
    bindings    String,
    severity    LowCardinality(String),
    message     String,
    timestamp   DateTime64(3, 'UTC'),
    poller_id   String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (source_ip, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;
