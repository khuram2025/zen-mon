-- Migration 016 (ClickHouse): Host metrics for server monitoring (agent + agentless)
--
-- Conventions match init-clickhouse.sql:
--   database = zenplus
--   timestamp column = `timestamp`, type DateTime64(3, 'UTC')
--   MergeTree + monthly partition + TTL
-- Safe to re-run.

-- ─── CPU metrics ───
-- One row per (server, timestamp). per_core stored as Array(Float32) for
-- aggregate display; total is the canonical scalar.
CREATE TABLE IF NOT EXISTS zenplus.host_cpu_metrics (
    server_id     UUID,
    agent_id      UUID,
    timestamp     DateTime64(3, 'UTC'),
    cpu_total_pct Float32,
    cpu_user_pct  Float32,
    cpu_system_pct Float32,
    cpu_iowait_pct Float32,
    cpu_steal_pct Float32,
    cpu_idle_pct  Float32,
    per_core      Array(Float32),
    load_1        Float32,
    load_5        Float32,
    load_15       Float32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Memory metrics ───
CREATE TABLE IF NOT EXISTS zenplus.host_memory_metrics (
    server_id        UUID,
    agent_id         UUID,
    timestamp        DateTime64(3, 'UTC'),
    total_bytes      UInt64,
    used_bytes       UInt64,
    available_bytes  UInt64,
    cached_bytes     UInt64,
    committed_bytes  UInt64,
    swap_total_bytes UInt64,
    swap_used_bytes  UInt64,
    used_pct         Float32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Filesystem metrics ───
CREATE TABLE IF NOT EXISTS zenplus.host_filesystem_metrics (
    server_id     UUID,
    agent_id      UUID,
    mount         LowCardinality(String),
    fs_type       LowCardinality(String),
    timestamp     DateTime64(3, 'UTC'),
    total_bytes   UInt64,
    used_bytes    UInt64,
    free_bytes    UInt64,
    used_pct      Float32,
    inodes_total  UInt64,
    inodes_used   UInt64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, mount, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Disk IO metrics ───
CREATE TABLE IF NOT EXISTS zenplus.host_disk_io_metrics (
    server_id      UUID,
    agent_id       UUID,
    device         LowCardinality(String),
    timestamp      DateTime64(3, 'UTC'),
    read_bytes_ps  Float64,
    write_bytes_ps Float64,
    read_iops      Float64,
    write_iops     Float64,
    queue_length   Float32,
    util_pct       Float32,
    avg_read_ms    Float32,
    avg_write_ms   Float32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, device, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Network metrics ───
CREATE TABLE IF NOT EXISTS zenplus.host_network_metrics (
    server_id      UUID,
    agent_id       UUID,
    if_name        LowCardinality(String),
    timestamp      DateTime64(3, 'UTC'),
    rx_bytes_ps    Float64,
    tx_bytes_ps    Float64,
    rx_packets_ps  Float64,
    tx_packets_ps  Float64,
    rx_errors_ps   Float64,
    tx_errors_ps   Float64,
    rx_dropped_ps  Float64,
    tx_dropped_ps  Float64,
    is_up          UInt8
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, if_name, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Process metrics (short retention; high cardinality) ───
CREATE TABLE IF NOT EXISTS zenplus.host_process_metrics (
    server_id     UUID,
    agent_id      UUID,
    process_name  LowCardinality(String),
    timestamp     DateTime64(3, 'UTC'),
    pid           Int32,
    cpu_pct       Float32,
    memory_bytes  UInt64,
    thread_count  UInt32,
    handle_count  UInt32,
    user_name     LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, process_name, timestamp)
TTL toDateTime(timestamp) + INTERVAL 14 DAY DELETE;

-- ─── Service state change log ───
CREATE TABLE IF NOT EXISTS zenplus.host_service_state (
    server_id    UUID,
    agent_id     UUID,
    service_name LowCardinality(String),
    timestamp    DateTime64(3, 'UTC'),
    state        LowCardinality(String),    -- running|stopped|paused|start_pending|...
    start_mode   LowCardinality(String),    -- auto|manual|disabled
    pid          Int32,
    exit_code    Int32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, service_name, timestamp)
TTL toDateTime(timestamp) + INTERVAL 60 DAY DELETE;

-- ─── Event log summary (counts per window) ───
CREATE TABLE IF NOT EXISTS zenplus.host_event_log_summary (
    server_id    UUID,
    agent_id     UUID,
    log_name     LowCardinality(String),    -- System|Application|Security|...
    level        LowCardinality(String),    -- critical|error|warning|information|verbose
    timestamp    DateTime64(3, 'UTC'),
    event_count  UInt32,
    sample_ids   Array(Int64)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, log_name, level, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── Agent self-health metrics ───
CREATE TABLE IF NOT EXISTS zenplus.agent_health_metrics (
    agent_id          UUID,
    server_id         UUID,
    timestamp         DateTime64(3, 'UTC'),
    cpu_pct           Float32,
    memory_bytes      UInt64,
    queue_depth       UInt32,
    spool_bytes       UInt64,
    upload_lag_ms     UInt32,
    config_apply_ok   UInt8,
    last_error        String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (agent_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

-- ─── 5-minute CPU rollup (90-day retention) ───
CREATE TABLE IF NOT EXISTS zenplus.host_cpu_metrics_5m (
    server_id     UUID,
    timestamp     DateTime64(3, 'UTC'),
    avg_total_pct Float32,
    max_total_pct Float32,
    min_total_pct Float32,
    avg_iowait    Float32,
    sample_count  UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_cpu_metrics_5m_mv
TO zenplus.host_cpu_metrics_5m
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

-- ─── 5-minute memory rollup ───
CREATE TABLE IF NOT EXISTS zenplus.host_memory_metrics_5m (
    server_id    UUID,
    timestamp    DateTime64(3, 'UTC'),
    avg_used_pct Float32,
    max_used_pct Float32,
    avg_used_bytes Float64,
    sample_count UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_memory_metrics_5m_mv
TO zenplus.host_memory_metrics_5m
AS SELECT
    server_id,
    toStartOfFiveMinutes(timestamp) AS timestamp,
    avg(used_pct) AS avg_used_pct,
    max(used_pct) AS max_used_pct,
    avg(used_bytes) AS avg_used_bytes,
    count() AS sample_count
FROM zenplus.host_memory_metrics
GROUP BY server_id, timestamp;

-- ─── 5-minute filesystem rollup ───
CREATE TABLE IF NOT EXISTS zenplus.host_filesystem_metrics_5m (
    server_id    UUID,
    mount        LowCardinality(String),
    timestamp    DateTime64(3, 'UTC'),
    avg_used_pct Float32,
    max_used_pct Float32,
    avg_used_bytes Float64,
    sample_count UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, mount, timestamp)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_filesystem_metrics_5m_mv
TO zenplus.host_filesystem_metrics_5m
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

-- ─── 5-minute network rollup ───
CREATE TABLE IF NOT EXISTS zenplus.host_network_metrics_5m (
    server_id      UUID,
    if_name        LowCardinality(String),
    timestamp      DateTime64(3, 'UTC'),
    avg_rx_bps     Float64,
    avg_tx_bps     Float64,
    max_rx_bps     Float64,
    max_tx_bps     Float64,
    sum_rx_errors  Float64,
    sum_tx_errors  Float64,
    sample_count   UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, if_name, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.host_network_metrics_5m_mv
TO zenplus.host_network_metrics_5m
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
