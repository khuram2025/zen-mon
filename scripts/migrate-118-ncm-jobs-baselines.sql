ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS schedule_timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS last_scheduled_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS ncm_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    config_types TEXT[] NOT NULL,
    trigger TEXT NOT NULL,
    actor_id UUID,
    actor_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','success','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ,
    lease_token UUID,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_code TEXT,
    results JSONB NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_jobs_active_device ON ncm_jobs(device_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS ncm_jobs_due ON ncm_jobs(available_at) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS ncm_baselines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    version_id UUID NOT NULL REFERENCES device_configs(id) ON DELETE RESTRICT,
    config_type TEXT NOT NULL,
    name TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id UUID,
    actor_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_baselines_active ON ncm_baselines(device_id,config_type) WHERE retired_at IS NULL;
