-- Migration 012 (ClickHouse): repair ping rollups used by larger device
-- detail windows. Safe to re-run, but backfills only when target tables are
-- empty to avoid duplicate aggregate rows.

CREATE TABLE IF NOT EXISTS zenplus.ping_metrics_5m (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    avg_rtt_ms      Float64,
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    avg_packet_loss Float32,
    avg_jitter_ms   Float64,
    uptime_pct      Float32,
    sample_count    UInt32,
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE TABLE IF NOT EXISTS zenplus.ping_metrics_1h (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    avg_rtt_ms      Float64,
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    p95_rtt_ms      Float64,
    avg_packet_loss Float32,
    avg_jitter_ms   Float64,
    uptime_pct      Float32,
    sample_count    UInt32,
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

DROP TABLE IF EXISTS zenplus.ping_metrics_1h_mv;
DROP TABLE IF EXISTS zenplus.ping_metrics_5m_mv;

CREATE MATERIALIZED VIEW zenplus.ping_metrics_5m_mv
TO zenplus.ping_metrics_5m
AS SELECT
    device_id,
    bucket AS timestamp,
    avg(rtt_ms)          AS avg_rtt_ms,
    min(min_rtt_ms)      AS min_rtt_ms,
    max(max_rtt_ms)      AS max_rtt_ms,
    avg(packet_loss)     AS avg_packet_loss,
    avg(jitter_ms)       AS avg_jitter_ms,
    avg(is_up)           AS uptime_pct,
    count()              AS sample_count,
    any(ip_address)      AS ip_address
FROM (
    SELECT
        device_id,
        toStartOfFiveMinutes(timestamp) AS bucket,
        rtt_ms,
        min_rtt_ms,
        max_rtt_ms,
        packet_loss,
        jitter_ms,
        is_up,
        ip_address
    FROM zenplus.ping_metrics
)
GROUP BY device_id, bucket;

INSERT INTO zenplus.ping_metrics_5m
SELECT
    device_id,
    bucket AS timestamp,
    avg(rtt_ms)          AS avg_rtt_ms,
    min(min_rtt_ms)      AS min_rtt_ms,
    max(max_rtt_ms)      AS max_rtt_ms,
    avg(packet_loss)     AS avg_packet_loss,
    avg(jitter_ms)       AS avg_jitter_ms,
    avg(is_up)           AS uptime_pct,
    count()              AS sample_count,
    any(ip_address)      AS ip_address
FROM (
    SELECT
        device_id,
        toStartOfFiveMinutes(timestamp) AS bucket,
        rtt_ms,
        min_rtt_ms,
        max_rtt_ms,
        packet_loss,
        jitter_ms,
        is_up,
        ip_address
    FROM zenplus.ping_metrics
    WHERE timestamp >= now() - INTERVAL 90 DAY
      AND (SELECT count() FROM zenplus.ping_metrics_5m) = 0
)
GROUP BY device_id, bucket;

CREATE MATERIALIZED VIEW zenplus.ping_metrics_1h_mv
TO zenplus.ping_metrics_1h
AS SELECT
    device_id,
    bucket AS timestamp,
    avg(src_avg_rtt_ms)              AS avg_rtt_ms,
    min(src_min_rtt_ms)              AS min_rtt_ms,
    max(src_max_rtt_ms)              AS max_rtt_ms,
    quantile(0.95)(src_avg_rtt_ms)   AS p95_rtt_ms,
    avg(src_packet_loss)             AS avg_packet_loss,
    avg(src_jitter_ms)               AS avg_jitter_ms,
    avg(src_uptime_pct)              AS uptime_pct,
    sum(src_sample_count)            AS sample_count,
    any(src_ip_address)              AS ip_address
FROM (
    SELECT
        device_id,
        toStartOfHour(timestamp) AS bucket,
        avg_rtt_ms AS src_avg_rtt_ms,
        min_rtt_ms AS src_min_rtt_ms,
        max_rtt_ms AS src_max_rtt_ms,
        avg_packet_loss AS src_packet_loss,
        avg_jitter_ms AS src_jitter_ms,
        uptime_pct AS src_uptime_pct,
        sample_count AS src_sample_count,
        ip_address AS src_ip_address
    FROM zenplus.ping_metrics_5m
)
GROUP BY device_id, bucket;

INSERT INTO zenplus.ping_metrics_1h
SELECT
    device_id,
    bucket AS timestamp,
    avg(rtt_ms)              AS avg_rtt_ms,
    min(min_rtt_ms)          AS min_rtt_ms,
    max(max_rtt_ms)          AS max_rtt_ms,
    quantile(0.95)(rtt_ms)   AS p95_rtt_ms,
    avg(packet_loss)         AS avg_packet_loss,
    avg(jitter_ms)           AS avg_jitter_ms,
    avg(is_up)               AS uptime_pct,
    count()                  AS sample_count,
    any(ip_address)          AS ip_address
FROM (
    SELECT
        device_id,
        toStartOfHour(timestamp) AS bucket,
        rtt_ms,
        min_rtt_ms,
        max_rtt_ms,
        packet_loss,
        jitter_ms,
        is_up,
        ip_address
    FROM zenplus.ping_metrics
    WHERE timestamp >= now() - INTERVAL 365 DAY
      AND (SELECT count() FROM zenplus.ping_metrics_1h) = 0
)
GROUP BY device_id, bucket;
