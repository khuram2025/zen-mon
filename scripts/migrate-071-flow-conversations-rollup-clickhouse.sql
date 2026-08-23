-- Hourly conversation rollup for NetFlow analytics (ClickHouse).
--
-- Month-scale dashboard queries (top conversations / talkers / endpoints,
-- protocol and application breakdowns) were scanning the raw flow_records
-- table — ~30M rows/day per appliance, so a 30-day window is a 600-900M-row
-- scan repeated by every card on the page. This rollup pre-aggregates flows
-- per (hour, exporter, src, dst, protocol, dst_port) — roughly 6-8x fewer
-- rows with the per-pair sums already computed — and the API routes long
-- windows to it when the active drill-down filters permit (no per-flow
-- ifIndex / DSCP / TCP-flag predicates, which only exist on raw records).
--
-- Retention outlives flow_records' 30 days so month views stay complete.
--
-- Historical backfill deliberately does NOT happen here: a fleet appliance
-- would need minutes of INSERT..SELECT, blowing the updater's statement
-- timeout, and an unguarded INSERT would make this file non-replay-safe.
-- The API server backfills missing hours idempotently at startup
-- (app/services/netflow_rollup.py); this file only creates the objects.

CREATE TABLE IF NOT EXISTS zenplus.flow_conversations_1h (
    timestamp     DateTime('UTC'),
    exporter_ip   IPv4,
    src_addr      IPv4,
    dst_addr      IPv4,
    protocol      UInt8,
    dst_port      UInt16,
    bytes         SimpleAggregateFunction(sum, UInt64),
    packets       SimpleAggregateFunction(sum, UInt64),
    flows         SimpleAggregateFunction(sum, UInt64),
    src_ports     AggregateFunction(groupUniqArray(10), UInt16),
    input_snmp    AggregateFunction(groupUniqArray(10), UInt16),
    output_snmp   AggregateFunction(groupUniqArray(10), UInt16),
    first_seen    SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    last_seen     SimpleAggregateFunction(max, DateTime64(3, 'UTC')),
    received_at   SimpleAggregateFunction(max, DateTime64(3, 'UTC')),
    duration_ms   SimpleAggregateFunction(sum, Int64),
    tcp_flags     SimpleAggregateFunction(groupBitOr, UInt64)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)
TTL timestamp + INTERVAL 90 DAY DELETE;

-- The inner subquery renames every raw column before aggregation. Without it,
-- `toStartOfHour(timestamp) AS timestamp` would shadow the raw column and
-- min/max(timestamp) would resolve to the bucket, not the flow time.
-- The WHERE clause mirrors the API's corrupt-flow guard (see _scope in
-- app/api/v1/netflow.py) so rollup and raw aggregates agree; the hourly
-- backfill healer compares row counts under the same predicate.
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_conversations_1h_mv
TO zenplus.flow_conversations_1h
AS SELECT
    hour_bucket AS timestamp,
    exporter_ip,
    src_addr,
    dst_addr,
    protocol,
    dst_port,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flows,
    groupUniqArrayState(10)(raw_src_port) AS src_ports,
    groupUniqArrayState(10)(raw_input_snmp) AS input_snmp,
    groupUniqArrayState(10)(raw_output_snmp) AS output_snmp,
    min(raw_ts) AS first_seen,
    max(raw_ts) AS last_seen,
    max(raw_received_at) AS received_at,
    sum(raw_duration_ms) AS duration_ms,
    groupBitOr(toUInt64(raw_tcp_flags)) AS tcp_flags
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        timestamp AS raw_ts,
        received_at AS raw_received_at,
        exporter_ip,
        src_addr,
        dst_addr,
        protocol,
        dst_port,
        src_port AS raw_src_port,
        input_snmp AS raw_input_snmp,
        output_snmp AS raw_output_snmp,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms,
        tcp_flags AS raw_tcp_flags
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
)
GROUP BY hour_bucket, exporter_ip, src_addr, dst_addr, protocol, dst_port;
