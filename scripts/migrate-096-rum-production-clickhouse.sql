-- Production browser-RUM event model and 90-day five-minute analytics rollup.

ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS event_id String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS sdk_version LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS service_version LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS browser_version LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS os LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS action_name String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS action_type LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS target String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS duration_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS resource_url String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS resource_type LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS method LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS status_code UInt16 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS transfer_size UInt64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS encoded_body_size UInt64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS error_type LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS error_stack String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS error_source String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS error_fingerprint String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS end_reason LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS is_final UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS sample_rate Float32 DEFAULT 1;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS sampled UInt8 DEFAULT 1;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS vital_attribution Map(LowCardinality(String), String) DEFAULT map() CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_lcp UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_inp UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_cls UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_fcp UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_ttfb UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_load UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS dedupe_id String DEFAULT '' CODEC(ZSTD(1));
ALTER TABLE zenplus.apm_rum_events ADD INDEX IF NOT EXISTS idx_rum_event_id event_id TYPE bloom_filter(0.001) GRANULARITY 1;
ALTER TABLE zenplus.apm_rum_events ADD INDEX IF NOT EXISTS idx_rum_dedupe_id dedupe_id TYPE bloom_filter(0.001) GRANULARITY 1;
-- Intake uses synchronous inserts and a stable token.  ClickHouse 24.x cannot
-- consistently deduplicate async inserts through dependent materialized views,
-- so the application writes the raw and rollup tables explicitly.
ALTER TABLE zenplus.apm_rum_events
    MODIFY SETTING non_replicated_deduplication_window = 100000;
-- CREATE TABLE AS SELECT backfills existing raw history only when the table is
-- first created; IF NOT EXISTS makes migration replay safe.  New batches are
-- dual-written by intake with an independent stable deduplication token.
CREATE TABLE IF NOT EXISTS zenplus.apm_rum_metrics_5m
(
    timestamp       DateTime('UTC'),
    application_id  LowCardinality(String),
    env             LowCardinality(String),
    service_version LowCardinality(String),
    view_name       LowCardinality(String),
    browser         LowCardinality(String),
    browser_version LowCardinality(String),
    os              LowCardinality(String),
    device_type     LowCardinality(String),
    country         LowCardinality(String),
    events          SimpleAggregateFunction(sum, UInt64),
    errors          SimpleAggregateFunction(sum, UInt64),
    sampled_errors  SimpleAggregateFunction(sum, UInt64),
    unsampled_errors SimpleAggregateFunction(sum, UInt64),
    resources       SimpleAggregateFunction(sum, UInt64),
    resource_failures SimpleAggregateFunction(sum, UInt64),
    actions         SimpleAggregateFunction(sum, UInt64),
    long_tasks      SimpleAggregateFunction(sum, UInt64),
    sessions        AggregateFunction(uniqCombined64, String),
    error_sessions  AggregateFunction(uniqCombined64, String),
    views           AggregateFunction(uniqCombined64, String),
    lcp             AggregateFunction(quantileTDigest(0.75), Float64),
    inp             AggregateFunction(quantileTDigest(0.75), Float64),
    cls             AggregateFunction(quantileTDigest(0.75), Float64),
    fcp             AggregateFunction(quantileTDigest(0.75), Float64),
    ttfb            AggregateFunction(quantileTDigest(0.75), Float64),
    load_ms         AggregateFunction(quantileTDigest(0.75), Float64),
    lcp_samples     SimpleAggregateFunction(sum, UInt64),
    inp_samples     SimpleAggregateFunction(sum, UInt64),
    cls_samples     SimpleAggregateFunction(sum, UInt64),
    fcp_samples     SimpleAggregateFunction(sum, UInt64),
    ttfb_samples    SimpleAggregateFunction(sum, UInt64),
    load_samples    SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (application_id, env, service_version, view_name, browser, browser_version, os, device_type, country, timestamp)
TTL timestamp + toIntervalDay(90)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1,
         non_replicated_deduplication_window = 100000
AS SELECT
    toStartOfFiveMinutes(timestamp) AS timestamp,
    application_id,
    env,
    service_version,
    view_name,
    browser,
    browser_version,
    os,
    device_type,
    country,
    count() AS events,
    countIf(event_type = 'error') AS errors,
    countIf(event_type = 'error' AND sampled = 1) AS sampled_errors,
    countIf(event_type = 'error' AND sampled = 0) AS unsampled_errors,
    countIf(event_type = 'resource' AND sampled = 1) AS resources,
    countIf(event_type = 'resource' AND sampled = 1 AND (status_code >= 400 OR attributes['failed'] = 'true')) AS resource_failures,
    countIf(event_type = 'action' AND sampled = 1) AS actions,
    countIf(event_type = 'long_task' AND sampled = 1) AS long_tasks,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id), sampled = 1
    ) AS sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id),
        event_type = 'error' AND sampled = 1
    ) AS error_sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), view_id),
        sampled = 1 AND event_type = 'view'
            AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))
    ) AS views,
    quantileTDigestStateIf(0.75)(r.lcp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp,
    quantileTDigestStateIf(0.75)(r.inp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp,
    quantileTDigestStateIf(0.75)(r.cls, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls,
    quantileTDigestStateIf(0.75)(r.fcp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp,
    quantileTDigestStateIf(0.75)(r.ttfb, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb,
    quantileTDigestStateIf(0.75)(r.load_ms, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_ms,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_samples
FROM zenplus.apm_rum_events AS r
GROUP BY timestamp, application_id, env, service_version, view_name, browser, browser_version, os, device_type, country;
