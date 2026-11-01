CREATE TABLE IF NOT EXISTS zenplus.host_event_details
(
    server_id UUID,
    agent_id UUID,
    timestamp DateTime64(3, 'UTC'),
    received_at DateTime64(3, 'UTC'),
    event_uid String,
    group_key String,
    log_name LowCardinality(String),
    provider LowCardinality(String),
    event_id UInt32,
    record_id UInt64,
    level LowCardinality(String),
    component String,
    message String,
    fields String,
    message_truncated UInt8
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(timestamp)
ORDER BY (server_id, timestamp, event_uid)
TTL toDateTime(timestamp) + toIntervalDay(14)
SETTINGS non_replicated_deduplication_window = 10000;
