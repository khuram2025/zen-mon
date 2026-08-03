-- Backward-compatible flow classification for richer capture views.
-- Existing directions read as unknown. Historical rows were connection-only,
-- so their kind defaults to connection; newer agents distinguish listeners
-- and connectionless endpoints explicitly.

ALTER TABLE zenplus.host_network_flows
    ADD COLUMN IF NOT EXISTS direction LowCardinality(String) DEFAULT 'unknown';

ALTER TABLE zenplus.host_network_flows
    ADD COLUMN IF NOT EXISTS kind LowCardinality(String) DEFAULT 'connection';
