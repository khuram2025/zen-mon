-- migrate-086-apm-p3-clickhouse.sql
-- Phase 3 APM storage: browser RUM and continuous profiles.

CREATE TABLE IF NOT EXISTS zenplus.apm_rum_events
(
    timestamp        DateTime64(3, 'UTC') CODEC(Delta(8), ZSTD(1)),
    application_id   LowCardinality(String),
    service_name     LowCardinality(String),
    env              LowCardinality(String),
    event_type       LowCardinality(String),
    session_id       String CODEC(ZSTD(1)),
    view_id          String CODEC(ZSTD(1)),
    view_name        LowCardinality(String),
    url              String CODEC(ZSTD(1)),
    user_id          String CODEC(ZSTD(1)),
    country          LowCardinality(String),
    browser          LowCardinality(String),
    device_type      LowCardinality(String),
    lcp              Float64,
    inp              Float64,
    cls              Float64,
    fcp              Float64,
    ttfb             Float64,
    load_ms          Float64,
    error_message    String CODEC(ZSTD(1)),
    backend_trace_id String CODEC(ZSTD(1)),
    attributes       Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ts_bucket        UInt32,
    INDEX idx_rum_session session_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_rum_trace backend_trace_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (application_id, view_name, ts_bucket, session_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(14)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zenplus.apm_profiles
(
    timestamp       DateTime64(3, 'UTC') CODEC(Delta(8), ZSTD(1)),
    profile_id      UUID,
    service_name    LowCardinality(String),
    env             LowCardinality(String),
    service_version LowCardinality(String),
    profile_type    LowCardinality(String),
    duration_nano   UInt64,
    sample_count    UInt64,
    encoding        LowCardinality(String),
    profile_data    String CODEC(ZSTD(3)),
    trace_id        String CODEC(ZSTD(1)),
    span_id         String CODEC(ZSTD(1)),
    attributes      Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ts_bucket       UInt32,
    INDEX idx_prof_trace trace_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, profile_type, ts_bucket, timestamp, profile_id)
TTL toDateTime(timestamp) + toIntervalDay(14)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
