-- Migration 011 (ClickHouse): repair SNMP rollups used by device detail
-- charts and ensure the device activity status table exists.
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS zenplus.device_status_log (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    old_status      String,
    new_status      String,
    reason          String,
    duration_sec    UInt64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

DROP VIEW IF EXISTS zenplus.snmp_metrics_5m_mv;

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

INSERT INTO zenplus.snmp_metrics_5m
SELECT
    device_id,
    metric_key,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(value)   AS avg_value,
    min(value)   AS min_value,
    max(value)   AS max_value,
    count()      AS sample_count
FROM zenplus.snmp_metrics
WHERE timestamp >= now() - INTERVAL 90 DAY
  AND (SELECT count() FROM zenplus.snmp_metrics_5m) = 0
GROUP BY device_id, metric_key, timestamp;

DROP VIEW IF EXISTS zenplus.snmp_if_metrics_5m_mv;

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
