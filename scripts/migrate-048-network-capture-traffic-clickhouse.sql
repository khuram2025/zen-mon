-- Additive interface-traffic store for appliances that already recorded the
-- original migrate-045 flow-only migration. Fresh installs run this migration
-- immediately after 045; CREATE IF NOT EXISTS keeps both paths safe.

CREATE TABLE IF NOT EXISTS zenplus.host_network_traffic_samples
(
    capture_id                 UUID,
    server_id                  UUID,
    agent_id                   UUID,
    observed_at                DateTime64(3),
    interface                  String,
    interface_index            UInt32,
    rx_bytes                   UInt64,
    tx_bytes                   UInt64,
    rx_bps                     Float64,
    tx_bps                     Float64,
    peak_rx_bps                Float64,
    peak_tx_bps                Float64,
    link_speed_bps             UInt64,
    receive_link_speed_bps     UInt64,
    transmit_link_speed_bps    UInt64,
    rx_utilization_pct         Nullable(Float64),
    tx_utilization_pct         Nullable(Float64),
    ingested_at                DateTime64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (capture_id, interface, observed_at)
TTL toDateTime(observed_at) + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;
