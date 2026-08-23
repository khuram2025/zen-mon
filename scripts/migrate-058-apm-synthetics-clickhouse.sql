-- Migration 058 (ClickHouse): APM synthetics results + ingest quality stats.
-- Idempotent — applied automatically on update by updater/clickhouse_sync.py.

-- Per-run results for synthetic user-scenario monitors (apm_synthetic_monitors
-- in Postgres holds the definitions; this holds the execution history).
-- steps_json carries full per-step detail: [{name, ok, status_code, ms,
-- error, asserts: [{type, ok, detail}]}].
CREATE TABLE IF NOT EXISTS zenplus.apm_synthetic_results (
    monitor_id   UUID,
    timestamp    DateTime,
    status       LowCardinality(String),   -- up | down
    success      UInt8,
    total_ms     UInt32,
    steps_total  UInt16,
    steps_passed UInt16,
    failed_step  String DEFAULT '',
    error        String DEFAULT '',
    steps_json   String DEFAULT '[]',
    location     LowCardinality(String) DEFAULT 'appliance'
) ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (monitor_id, timestamp)
TTL timestamp + toIntervalDay(90)
SETTINGS index_granularity = 8192;

-- Cluster-wide OTLP ingest counters, written by the batch writer on each
-- flush as per-minute deltas. SummingMergeTree collapses rows from multiple
-- uvicorn workers into one row per minute. Replaces the old per-process
-- in-memory-only view for data-quality monitoring.
CREATE TABLE IF NOT EXISTS zenplus.apm_ingest_stats (
    timestamp DateTime,
    accepted  UInt64,
    rejected  UInt64,
    dropped   UInt64,
    skewed    UInt64,
    flushes   UInt32
) ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp)
TTL timestamp + toIntervalDay(30)
SETTINGS index_granularity = 8192;
