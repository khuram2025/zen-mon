-- ─── Fix service_metrics_5m_mv: unblock all service-check metric ingestion ───
--
-- The deployed view projected the bucket as `toStartOfFiveMinutes(timestamp) AS timestamp`
-- but grouped by the expression `toStartOfFiveMinutes(timestamp)`. Inside GROUP BY the
-- bare `timestamp` resolves to the SELECT alias, so the grouping key no longer matched the
-- projected expression and ClickHouse rejected the view with
--   code 215 (NOT_AN_AGGREGATE): Column zenplus.service_metrics.timestamp is not under
--   aggregate function and not in GROUP BY keys
-- Because a materialized view is evaluated as part of the INSERT, that error failed every
-- write to zenplus.service_metrics — the poller logged "Failed to flush service metrics
-- batch" once per flush and raw probe samples stopped being stored entirely.
--
-- Grouping by the alias (`GROUP BY service_check_id, timestamp`) resolves to the same
-- bucket expression that is projected, which parses and keeps the target column name.

DROP VIEW IF EXISTS zenplus.service_metrics_5m_mv;

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
