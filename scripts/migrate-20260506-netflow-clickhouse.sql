-- ZenPlus NetFlow ClickHouse migration
-- Adds raw normalized flow storage and a 5-minute traffic rollup.

CREATE TABLE IF NOT EXISTS zenplus.flow_records (
    timestamp          DateTime64(3, 'UTC'),
    received_at        DateTime64(3, 'UTC'),
    collector_id       LowCardinality(String),
    exporter_ip        IPv4,
    flow_version       UInt8,
    flow_sequence      UInt32,
    engine_type        UInt8,
    engine_id          UInt8,
    sampling_interval  UInt32,
    src_addr           IPv4,
    dst_addr           IPv4,
    next_hop           IPv4,
    input_snmp         UInt16,
    output_snmp        UInt16,
    packets            UInt64,
    bytes              UInt64,
    first_switched_ms  UInt64,
    last_switched_ms   UInt64,
    src_port           UInt16,
    dst_port           UInt16,
    tcp_flags          UInt8,
    protocol           UInt8,
    tos                UInt8,
    src_as             UInt32,
    dst_as             UInt32,
    src_mask           UInt8,
    dst_mask           UInt8
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS zenplus.flow_traffic_5m (
    timestamp     DateTime64(3, 'UTC'),
    exporter_ip   IPv4,
    protocol      UInt8,
    dst_port      UInt16,
    bytes         UInt64,
    packets       UInt64,
    flow_count    UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, exporter_ip, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_traffic_5m_mv
TO zenplus.flow_traffic_5m
AS SELECT
    toStartOfFiveMinutes(timestamp) AS timestamp,
    exporter_ip,
    protocol,
    dst_port,
    sum(bytes) AS bytes,
    sum(packets) AS packets,
    count() AS flow_count
FROM zenplus.flow_records
GROUP BY timestamp, exporter_ip, protocol, dst_port;
