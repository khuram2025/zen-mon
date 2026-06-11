-- Migration 030 (ClickHouse): host/server metric time-series tables.
--
-- These back the agent ingestion pipeline (/api/v1/agents/results/host →
-- host_metric_service.py) and the server-detail charts.  Until now they only
-- existed on the build box; fresh installs had no host_* tables at all.
--
-- Raw tables keep 14-60 days; 5-minute rollups (fed by materialized views)
-- keep 90-365 days for long-range charts.

CREATE TABLE IF NOT EXISTS zenplus.host_cpu_metrics
(
    server_id UUID,
    agent_id UUID,
    timestamp DateTime64(3, 'UTC'),
    cpu_total_pct Float32,
    cpu_user_pct Float32,
    cpu_system_pct Float32,
    cpu_iowait_pct Float32,
    cpu_steal_pct Float32,
    cpu_idle_pct Float32,
    per_core Array(Float32),
    load_1 Float32,
    load_5 Float32,
    load_15 Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.host_memory_metrics
(
    server_id UUID,
    agent_id UUID,
    timestamp DateTime64(3, 'UTC'),
    total_bytes UInt64,
    used_bytes UInt64,
    available_bytes UInt64,
    cached_bytes UInt64,
    committed_bytes UInt64,
    swap_total_bytes UInt64,
    swap_used_bytes UInt64,
    used_pct Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.host_filesystem_metrics
(
    server_id UUID,
    agent_id UUID,
    mount LowCardinality(String),
    fs_type LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    total_bytes UInt64,
    used_bytes UInt64,
    free_bytes UInt64,
    used_pct Float32,
    inodes_total UInt64,
    inodes_used UInt64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, mount, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.host_disk_io_metrics
(
    server_id UUID,
    agent_id UUID,
    device LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    read_bytes_ps Float64,
    write_bytes_ps Float64,
    read_iops Float64,
    write_iops Float64,
    queue_length Float32,
    util_pct Float32,
    avg_read_ms Float32,
    avg_write_ms Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, device, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.host_network_metrics
(
    server_id UUID,
    agent_id UUID,
    if_name LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    rx_bytes_ps Float64,
    tx_bytes_ps Float64,
    rx_packets_ps Float64,
    tx_packets_ps Float64,
    rx_errors_ps Float64,
    tx_errors_ps Float64,
    rx_dropped_ps Float64,
    tx_dropped_ps Float64,
    is_up UInt8
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, if_name, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.host_process_metrics
(
    server_id UUID,
    agent_id UUID,
    process_name LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    pid Int32,
    cpu_pct Float32,
    memory_bytes UInt64,
    thread_count UInt32,
    handle_count UInt32,
    user_name LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, process_name, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(14);

CREATE TABLE IF NOT EXISTS zenplus.host_service_state
(
    server_id UUID,
    agent_id UUID,
    service_name LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    state LowCardinality(String),
    start_mode LowCardinality(String),
    pid Int32,
    exit_code Int32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, service_name, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(60);

CREATE TABLE IF NOT EXISTS zenplus.host_event_log_summary
(
    server_id UUID,
    agent_id UUID,
    log_name LowCardinality(String),
    level LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    event_count UInt32,
    sample_ids Array(Int64)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, log_name, level, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

CREATE TABLE IF NOT EXISTS zenplus.agent_health_metrics
(
    agent_id UUID,
    server_id UUID,
    timestamp DateTime64(3, 'UTC'),
    cpu_pct Float32,
    memory_bytes UInt64,
    queue_depth UInt32,
    spool_bytes UInt64,
    upload_lag_ms UInt32,
    config_apply_ok UInt8,
    last_error String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (agent_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);

-- ── 5-minute rollups ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS zenplus.host_cpu_metrics_5m
(
    server_id UUID,
    timestamp DateTime64(3, 'UTC'),
    avg_total_pct Float32,
    max_total_pct Float32,
    min_total_pct Float32,
    avg_iowait Float32,
    sample_count UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

CREATE TABLE IF NOT EXISTS zenplus.host_memory_metrics_5m
(
    server_id UUID,
    timestamp DateTime64(3, 'UTC'),
    avg_used_pct Float32,
    max_used_pct Float32,
    avg_used_bytes Float64,
    sample_count UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

CREATE TABLE IF NOT EXISTS zenplus.host_filesystem_metrics_5m
(
    server_id UUID,
    mount LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    avg_used_pct Float32,
    max_used_pct Float32,
    avg_used_bytes Float64,
    sample_count UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, mount, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(365);

CREATE TABLE IF NOT EXISTS zenplus.host_network_metrics_5m
(
    server_id UUID,
    if_name LowCardinality(String),
    timestamp DateTime64(3, 'UTC'),
    avg_rx_bps Float64,
    avg_tx_bps Float64,
    max_rx_bps Float64,
    max_tx_bps Float64,
    sum_rx_errors Float64,
    sum_tx_errors Float64,
    sample_count UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, if_name, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_cpu_metrics_5m_mv TO zenplus.host_cpu_metrics_5m
AS SELECT
    server_id,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(cpu_total_pct) AS avg_total_pct,
    max(cpu_total_pct) AS max_total_pct,
    min(cpu_total_pct) AS min_total_pct,
    avg(cpu_iowait_pct) AS avg_iowait,
    count() AS sample_count
FROM zenplus.host_cpu_metrics
GROUP BY server_id, timestamp;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_memory_metrics_5m_mv TO zenplus.host_memory_metrics_5m
AS SELECT
    server_id,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(used_pct) AS avg_used_pct,
    max(used_pct) AS max_used_pct,
    avg(used_bytes) AS avg_used_bytes,
    count() AS sample_count
FROM zenplus.host_memory_metrics
GROUP BY server_id, timestamp;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_filesystem_metrics_5m_mv TO zenplus.host_filesystem_metrics_5m
AS SELECT
    server_id,
    mount,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(used_pct) AS avg_used_pct,
    max(used_pct) AS max_used_pct,
    avg(used_bytes) AS avg_used_bytes,
    count() AS sample_count
FROM zenplus.host_filesystem_metrics
GROUP BY server_id, mount, timestamp;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_network_metrics_5m_mv TO zenplus.host_network_metrics_5m
AS SELECT
    server_id,
    if_name,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(rx_bytes_ps) AS avg_rx_bps,
    avg(tx_bytes_ps) AS avg_tx_bps,
    max(rx_bytes_ps) AS max_rx_bps,
    max(tx_bytes_ps) AS max_tx_bps,
    sum(rx_errors_ps) AS sum_rx_errors,
    sum(tx_errors_ps) AS sum_tx_errors,
    count() AS sample_count
FROM zenplus.host_network_metrics
GROUP BY server_id, if_name, timestamp;
