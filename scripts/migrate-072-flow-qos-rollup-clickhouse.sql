-- Hourly QoS/flag rollup for NetFlow analytics (ClickHouse).
--
-- Companion to migrate-071's conversation rollup. That table deliberately
-- drops ToS and per-flow TCP flags to keep its (pair-keyed) cardinality low,
-- which leaves the DSCP, TCP-flags and exporter-health cards scanning raw
-- flow_records on month windows. Those cards only slice by exporter,
-- protocol, DSCP and flag byte — so this rollup keys on exactly that. The
-- cardinality is tiny (hundreds of rows per hour, versus millions of raw
-- flows), making month-scale QoS queries effectively free.
--
-- Retention matches the conversation rollup so both outlive raw's 30 days.
-- Historical backfill happens in app/services/netflow_rollup.py, same as
-- migrate-071 and for the same reasons (updater timeout, replay safety).

CREATE TABLE IF NOT EXISTS zenplus.flow_qos_1h (
    timestamp     DateTime('UTC'),
    exporter_ip   IPv4,
    protocol      UInt8,
    tos           UInt8,
    tcp_flags     UInt8,
    bytes         SimpleAggregateFunction(sum, UInt64),
    packets       SimpleAggregateFunction(sum, UInt64),
    flows         SimpleAggregateFunction(sum, UInt64),
    empty_flows   SimpleAggregateFunction(sum, UInt64),
    duration_ms   SimpleAggregateFunction(sum, Int64),
    received_at   SimpleAggregateFunction(max, DateTime64(3, 'UTC'))
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, protocol, tos, tcp_flags)
TTL timestamp + INTERVAL 90 DAY DELETE;

-- Same corrupt-flow guard and inner-rename structure as the conversation MV
-- (see migrate-071 for why); the backfill healer compares counts under the
-- same predicate.
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_qos_1h_mv
TO zenplus.flow_qos_1h
AS SELECT
    hour_bucket AS timestamp,
    exporter_ip,
    protocol,
    tos,
    tcp_flags,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flows,
    countIf(raw_packets = 0) AS empty_flows,
    sum(raw_duration_ms) AS duration_ms,
    max(raw_received_at) AS received_at
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        received_at AS raw_received_at,
        exporter_ip,
        protocol,
        tos,
        tcp_flags,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
)
GROUP BY hour_bucket, exporter_ip, protocol, tos, tcp_flags;
