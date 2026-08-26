-- Preserve the process enrichment emitted by current server agents.
-- Every ALTER is replay-safe so the ClickHouse migration healer can rerun it.

ALTER TABLE zenplus.host_process_metrics
    ADD COLUMN IF NOT EXISTS cmdline String DEFAULT '';

ALTER TABLE zenplus.host_process_metrics
    ADD COLUMN IF NOT EXISTS started_at Nullable(DateTime64(3, 'UTC'));

ALTER TABLE zenplus.host_process_metrics
    ADD COLUMN IF NOT EXISTS state LowCardinality(String) DEFAULT 'running';

ALTER TABLE zenplus.host_process_metrics
    ADD COLUMN IF NOT EXISTS running UInt8 DEFAULT 1;

ALTER TABLE zenplus.host_process_metrics
    ADD COLUMN IF NOT EXISTS watchlisted UInt8 DEFAULT 0;
