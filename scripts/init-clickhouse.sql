-- ZenPlus ClickHouse Schema

CREATE DATABASE IF NOT EXISTS zenplus;

-- ─── Raw ping metrics (30-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.ping_metrics (
    device_id       UUID,
    timestamp       DateTime64(3, 'UTC'),
    is_up           UInt8,
    rtt_ms          Float64,
    packet_loss     Float32,
    jitter_ms       Float64,
    min_rtt_ms      Float64,
    max_rtt_ms      Float64,
    packets_sent    UInt16,
    packets_recv    UInt16,
    poller_id       String,
    ip_address      IPv4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (device_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- ─── 5-minute rollup (90-day retention) ───
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

-- ─── Materialized view: raw → 5m rollup ───
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.ping_metrics_5m_mv
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

-- ─── 1-hour rollup (1-year retention) ───
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

-- ─── Materialized view: 5m → 1h rollup ───
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.ping_metrics_1h_mv
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

-- ─── Service check raw metrics (30-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.service_metrics (
    service_check_id UUID,
    device_id        UUID,
    timestamp        DateTime64(3, 'UTC'),
    check_type       String,
    is_up            UInt8,
    response_ms      Float64,
    status_code      Nullable(UInt16),
    tls_days_remaining Nullable(Int32),
    tls_valid        Nullable(UInt8),
    content_matched  Nullable(UInt8),
    error_message    Nullable(String),
    poller_id        String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

-- ─── Service check 5-minute rollup (90-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.service_metrics_5m (
    service_check_id UUID,
    device_id        Nullable(UUID),
    timestamp        DateTime64(3, 'UTC'),
    check_type       LowCardinality(String),
    avg_response_ms  Float64,
    min_response_ms  Float64,
    max_response_ms  Float64,
    uptime_pct       Float32,
    sample_count     UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

-- ─── Materialized view: service_metrics → 5m rollup ───
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.service_metrics_5m_mv
TO zenplus.service_metrics_5m
AS SELECT
    service_check_id,
    any(device_id)                      AS device_id,
    toStartOfFiveMinutes(timestamp)     AS timestamp,
    any(check_type)                     AS check_type,
    avg(response_ms)                    AS avg_response_ms,
    min(response_ms)                    AS min_response_ms,
    max(response_ms)                    AS max_response_ms,
    avg(is_up)                          AS uptime_pct,
    count()                             AS sample_count
FROM zenplus.service_metrics
-- Group by the alias, not toStartOfFiveMinutes(timestamp): inside GROUP BY the bare column
-- resolves to the alias above, and repeating the expression makes the key differ from the
-- projection, which ClickHouse rejects with code 215 and fails every insert into the source.
GROUP BY service_check_id, timestamp;

-- ─── Service status change log (1-year retention) ───
CREATE TABLE IF NOT EXISTS zenplus.service_status_log (
    service_check_id UUID,
    device_id        UUID,
    timestamp        DateTime64(3, 'UTC'),
    check_type       String,
    old_status       String,
    new_status       String,
    reason           String,
    duration_sec     UInt64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_check_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

-- ─── Device status change log (1-year retention) ───
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

-- ─── NetFlow/IPFIX/sFlow: raw normalized flow records ───
-- One row per flow record from any export protocol (NetFlow v5/v9, IPFIX,
-- sFlow v5). Columns after dst_mask are schema v2 (migrate-137): they sit at the
-- end, in exactly the order migrate-137's ALTER appends them, so a fresh
-- install and an upgraded appliance end with the same table. IPv4 flows use the
-- IPv4 columns and leave the *6 columns at '::'; IPv6 flows do the reverse.
-- fields_present is the field-presence bitmask (0 = legacy row, unknown).
-- The hourly conversation/QoS rollups, the per-interface and per-exporter
-- rollups and the collector evidence tables are created by migrate-071, 072
-- and 137, which install.sh runs after this file.
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
    dst_mask           UInt8,
    input_if            UInt32 DEFAULT input_snmp,
    output_if           UInt32 DEFAULT output_snmp,
    ip_version          UInt8 DEFAULT 4,
    exporter_ip6        IPv6,
    src_addr6           IPv6,
    dst_addr6           IPv6,
    next_hop6           IPv6,
    fields_present      UInt32 DEFAULT 0,
    observation_domain  UInt32 DEFAULT 0,
    flow_direction      UInt8 DEFAULT 255,
    export_protocol     LowCardinality(String) DEFAULT '',
    app_id              UInt64 DEFAULT 0,
    fw_event            UInt8 DEFAULT 0,
    xlate_src_addr6     IPv6,
    xlate_dst_addr6     IPv6,
    xlate_src_port      UInt16 DEFAULT 0,
    xlate_dst_port      UInt16 DEFAULT 0,
    flow_end_reason     UInt8 DEFAULT 0
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, non_replicated_deduplication_window = 1000;

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

-- Identical to the view migrate-137 installs (volume guard, see there).
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_traffic_5m_mv
TO zenplus.flow_traffic_5m
AS SELECT
    slot_bucket AS timestamp,
    exporter_ip,
    protocol,
    dst_port,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flow_count
FROM (
    SELECT
        toStartOfFiveMinutes(timestamp) AS slot_bucket,
        exporter_ip,
        protocol,
        dst_port,
        bytes AS raw_bytes,
        packets AS raw_packets
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
      AND fw_event NOT IN (1, 3)
)
GROUP BY slot_bucket, exporter_ip, protocol, dst_port;
