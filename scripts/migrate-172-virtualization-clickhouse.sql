CREATE TABLE IF NOT EXISTS zenplus.virtualization_metrics (
    connector_id UUID,
    resource_id String,
    timestamp DateTime64(3, 'UTC'),
    metric LowCardinality(String),
    value Float64
) ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (connector_id, resource_id, metric, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;
