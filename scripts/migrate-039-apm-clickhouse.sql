-- migrate-039-apm-clickhouse.sql
-- ZenPlus Application Monitoring (APM) — AM-E1/E3/E4 ClickHouse schema.
--
-- Storage for OpenTelemetry-native traces and their derived signals:
--   * apm_spans            — raw spans (OTLP), 7-day raw retention
--   * apm_traces_resource  — resource-attribute side table (fingerprint -> labels)
--   * apm_span_metrics_5m  — RED rollups (request/error counts + tdigest latency state)
--   * apm_span_metrics_1h  — 1-hour RED rollups
--   * apm_service_graph    — service dependency edges with edge-RED
--   * apm_exceptions       — Sentry-style grouped exceptions (span events)
--
-- Auto-applied by updater/clickhouse_sync.py on every OTA update. EVERY statement
-- is CREATE ... IF NOT EXISTS and idempotent. This file must NEVER be added to
-- _LEGACY_BASELINE. See Documentation/ApplicationMonitier/03-ARCHITECTURE-AND-DATA-MODEL.md.
--
-- NOTE (deliberate deviation from the design sketch): the RED rollup tables use
-- AggregatingMergeTree (not SummingMergeTree). SummingMergeTree would *sum* the
-- duration_min/duration_max columns (meaningless) and does not state-merge the
-- tdigest. AggregatingMergeTree with SimpleAggregateFunction(sum/min/max) +
-- AggregateFunction(quantilesTDigest) is the correct engine and preserves the
-- design intent (mergeable percentiles, correct counters/min/max across 5m->1h).

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_spans — the core raw-span table
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_spans
(
    timestamp         DateTime64(9, 'UTC') CODEC(Delta(8), ZSTD(1)),
    trace_id          FixedString(32)      CODEC(ZSTD(1)),
    span_id           String               CODEC(ZSTD(1)),
    parent_span_id    String               CODEC(ZSTD(1)),
    name              LowCardinality(String),
    span_kind         Int8,
    span_kind_str     LowCardinality(String),
    service_name      LowCardinality(String),
    env               LowCardinality(String),
    service_version   LowCardinality(String),
    duration_nano     UInt64               CODEC(ZSTD(1)),
    status_code       LowCardinality(String),
    status_message    String               CODEC(ZSTD(1)),
    has_error         UInt8,
    http_method       LowCardinality(String),
    http_route        LowCardinality(String),
    http_status_code  UInt16,
    db_system         LowCardinality(String),
    db_operation      LowCardinality(String),
    db_statement      String               CODEC(ZSTD(1)),
    rpc_method        LowCardinality(String),
    attributes_string Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    attributes_number Map(LowCardinality(String), Float64),
    attributes_bool   Map(LowCardinality(String), UInt8),
    resource          String               CODEC(ZSTD(1)),
    resource_fingerprint String            CODEC(ZSTD(1)),
    events_ts         Array(DateTime64(9, 'UTC')),
    events_name       Array(LowCardinality(String)),
    events_attrs      Array(String)        CODEC(ZSTD(1)),
    links_trace_id    Array(FixedString(32)),
    links_span_id     Array(String),
    ts_bucket         UInt32,
    deployment_id     UUID,
    INDEX idx_trace_id       trace_id                      TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_span_attr_keys mapKeys(attributes_string)    TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_span_attr_vals mapValues(attributes_string)  TYPE bloom_filter(0.01)  GRANULARITY 1,
    INDEX idx_duration       duration_nano                 TYPE minmax              GRANULARITY 1,
    INDEX idx_http_route     http_route                    TYPE bloom_filter(0.01)  GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, name, ts_bucket, trace_id)
TTL toDateTime(timestamp) + toIntervalDay(7)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_traces_resource — resource side table (avoids full scans on resource filters)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_traces_resource
(
    fingerprint  String,
    labels       String,
    seen_at      DateTime64(3, 'UTC'),
    ts_bucket    UInt32
)
ENGINE = ReplacingMergeTree(seen_at)
PARTITION BY toYYYYMM(seen_at)
ORDER BY (fingerprint)
TTL toDateTime(seen_at) + toIntervalDay(7);

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_span_metrics_5m — RED rollup (computed from 100% of spans, pre-sampling)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_span_metrics_5m
(
    timestamp      DateTime64(3, 'UTC'),
    service_name   LowCardinality(String),
    operation      LowCardinality(String),
    span_kind      LowCardinality(String),
    env            LowCardinality(String),
    status_code    LowCardinality(String),
    request_count  SimpleAggregateFunction(sum, UInt64),
    error_count    SimpleAggregateFunction(sum, UInt64),
    duration_state AggregateFunction(quantilesTDigest(0.5, 0.75, 0.9, 0.95, 0.99), Float64),
    duration_sum   SimpleAggregateFunction(sum, Float64),
    duration_min   SimpleAggregateFunction(min, Float64),
    duration_max   SimpleAggregateFunction(max, Float64),
    sample_count   SimpleAggregateFunction(sum, UInt64),
    -- Apdex buckets at the default T=500ms (satisfied <=T, tolerating <=4T).
    -- Computed at insert from 100% of spans so apdex stays accurate under sampling.
    satisfied_count  SimpleAggregateFunction(sum, UInt64),
    tolerating_count SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_name, operation, span_kind, env, status_code, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

-- NB: bucket in a subquery so the outer column is a plain `timestamp` (matches
-- the target column BY NAME — ClickHouse maps TO-table MV columns by name) and
-- the outer GROUP BY has no alias/column shadowing.
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.apm_span_metrics_5m_mv
TO zenplus.apm_span_metrics_5m AS
SELECT
    timestamp,
    service_name, operation, span_kind, env, status_code,
    count()                                                  AS request_count,
    countIf(has_error = 1)                                   AS error_count,
    quantilesTDigestState(0.5, 0.75, 0.9, 0.95, 0.99)(duration_ms) AS duration_state,
    sum(duration_ms)                                         AS duration_sum,
    min(duration_ms)                                         AS duration_min,
    max(duration_ms)                                         AS duration_max,
    count()                                                  AS sample_count,
    countIf(duration_ms <= 500)                              AS satisfied_count,
    countIf(duration_ms > 500 AND duration_ms <= 2000)       AS tolerating_count
FROM (
    SELECT
        toStartOfFiveMinutes(timestamp) AS timestamp,
        service_name,
        name            AS operation,
        span_kind_str   AS span_kind,
        env,
        status_code,
        has_error,
        duration_nano / 1e6 AS duration_ms
    FROM zenplus.apm_spans
)
GROUP BY timestamp, service_name, operation, span_kind, env, status_code;

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_span_metrics_1h — 1-hour RED rollup (395-day retention)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_span_metrics_1h
(
    timestamp      DateTime64(3, 'UTC'),
    service_name   LowCardinality(String),
    operation      LowCardinality(String),
    span_kind      LowCardinality(String),
    env            LowCardinality(String),
    status_code    LowCardinality(String),
    request_count  SimpleAggregateFunction(sum, UInt64),
    error_count    SimpleAggregateFunction(sum, UInt64),
    duration_state AggregateFunction(quantilesTDigest(0.5, 0.75, 0.9, 0.95, 0.99), Float64),
    duration_sum   SimpleAggregateFunction(sum, Float64),
    duration_min   SimpleAggregateFunction(min, Float64),
    duration_max   SimpleAggregateFunction(max, Float64),
    sample_count   SimpleAggregateFunction(sum, UInt64),
    satisfied_count  SimpleAggregateFunction(sum, UInt64),
    tolerating_count SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_name, operation, span_kind, env, status_code, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(395);

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.apm_span_metrics_1h_mv
TO zenplus.apm_span_metrics_1h AS
SELECT
    timestamp,
    service_name, operation, span_kind, env, status_code,
    count()                                                  AS request_count,
    countIf(has_error = 1)                                   AS error_count,
    quantilesTDigestState(0.5, 0.75, 0.9, 0.95, 0.99)(duration_ms) AS duration_state,
    sum(duration_ms)                                         AS duration_sum,
    min(duration_ms)                                         AS duration_min,
    max(duration_ms)                                         AS duration_max,
    count()                                                  AS sample_count,
    countIf(duration_ms <= 500)                              AS satisfied_count,
    countIf(duration_ms > 500 AND duration_ms <= 2000)       AS tolerating_count
FROM (
    SELECT
        toStartOfHour(timestamp) AS timestamp,
        service_name,
        name            AS operation,
        span_kind_str   AS span_kind,
        env,
        status_code,
        has_error,
        duration_nano / 1e6 AS duration_ms
    FROM zenplus.apm_spans
)
GROUP BY timestamp, service_name, operation, span_kind, env, status_code;

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_service_graph — dependency edges with edge-RED (all-additive -> SummingMergeTree)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_service_graph
(
    timestamp        DateTime64(3, 'UTC'),
    client_service   LowCardinality(String),
    server_service   LowCardinality(String),
    env              LowCardinality(String),
    request_count    UInt64,
    error_count      UInt64,
    duration_sum_ms  Float64,
    sample_count     UInt32
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (client_service, server_service, env, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

-- Edge derivation (AM-E3): the Go collector's `servicegraph` connector is the
-- primary path; on the FastAPI fallback path edges are paired CLIENT/SERVER in
-- the app-layer ingest writer (within each batch, by trace_id + parent_span_id)
-- and inserted here directly. A ClickHouse self-join MV over the high-volume
-- spans table was intentionally NOT used (it would re-scan the whole table per
-- insert block and only see SERVER spans already persisted).

-- ─────────────────────────────────────────────────────────────────────────────
-- apm_exceptions — grouped exceptions (span events with exception.* attributes)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS zenplus.apm_exceptions
(
    timestamp         DateTime64(9, 'UTC') CODEC(Delta(8), ZSTD(1)),
    error_id          UUID,
    group_id          FixedString(16),
    trace_id          FixedString(32),
    span_id           String,
    service_name      LowCardinality(String),
    env               LowCardinality(String),
    service_version   LowCardinality(String),
    exception_type    LowCardinality(String),
    exception_message String               CODEC(ZSTD(1)),
    exception_stack   String               CODEC(ZSTD(1)),
    exception_escaped UInt8,
    http_route        LowCardinality(String),
    resource_tags     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ts_bucket         UInt32,
    INDEX idx_exc_group group_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_exc_trace trace_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, group_id, ts_bucket, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
