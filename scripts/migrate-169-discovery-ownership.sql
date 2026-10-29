-- Stop all legacy discovery executors before upgrading or rolling back.
-- NULL ownership denotes a never-claimed queued run or a legacy running run.
ALTER TABLE discovery_runs
    ADD COLUMN IF NOT EXISTS executor_id UUID,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_discovery_runs_expired_owner
    ON discovery_runs (lease_expires_at) WHERE status = 'running';
