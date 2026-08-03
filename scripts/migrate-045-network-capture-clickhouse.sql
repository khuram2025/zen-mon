-- Network capture flow store.
--
-- The Postgres side (migrate-043-network-capture.sql) holds one control row
-- per capture; the conversations themselves land here, because a single
-- 5-minute capture on a busy host is thousands of rows and the read pattern
-- is "aggregate everything for one capture_id".
--
-- This table was previously created by hand on the development appliance, so
-- the feature worked there and nowhere else: a fresh install had no table,
-- every agent upload failed on insert, and the capture UI reported the flow
-- store as unavailable. Only files matching migrate-*-clickhouse.sql are run
-- against ClickHouse by install.sh, hence the name.
--
-- ReplacingMergeTree keyed by the connection identity: the agent re-uploads a
-- cumulative snapshot of the whole flow set every 10s while a capture runs,
-- so later uploads must supersede earlier partials rather than accumulate.
-- observed_at is the version column, so the newest upload for a connection
-- wins. Reads still aggregate with max() to stay correct before a merge runs.

CREATE TABLE IF NOT EXISTS zenplus.host_network_flows
(
    capture_id      UUID,
    server_id       UUID,
    agent_id        UUID,
    observed_at     DateTime64(3),
    protocol        LowCardinality(String),
    local_ip        String,
    local_port      UInt32,
    remote_ip       String,
    remote_port     UInt32,
    pid             Int32,
    process_name    LowCardinality(String),
    service_name    String,
    state           LowCardinality(String),
    bytes_sent      UInt64,
    bytes_received  UInt64,
    bytes_known     UInt8,
    first_seen      DateTime64(3),
    last_seen       DateTime64(3),
    samples         UInt32
)
ENGINE = ReplacingMergeTree(observed_at)
ORDER BY (capture_id, protocol, local_ip, local_port, remote_ip, remote_port, pid)
TTL toDateTime(observed_at) + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;
