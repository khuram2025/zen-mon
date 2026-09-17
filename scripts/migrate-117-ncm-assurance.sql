-- NCM assurance: additive schema. Existing archives remain readable until the
-- application encryption maintenance command rewraps them in bounded batches.
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS content_enc BYTEA;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS validated BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS actor_id UUID;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS driver_version TEXT;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS notify_channels UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS freshness_hours INTEGER NOT NULL DEFAULT 24;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS eligible_override BOOLEAN;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS config_types TEXT[] NOT NULL DEFAULT '{running}';

CREATE TABLE IF NOT EXISTS ncm_backup_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    config_type TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL,
    actor_id UUID,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running','success','failed','interrupted')),
    validated BOOLEAN NOT NULL DEFAULT false,
    is_change BOOLEAN,
    version_id UUID REFERENCES device_configs(id) ON DELETE SET NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS ncm_runs_device_time ON ncm_backup_runs(device_id, started_at DESC);

CREATE TABLE IF NOT EXISTS ncm_notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    channel_id UUID NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed','disabled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    last_error TEXT,
    UNIQUE(event_key, channel_id)
);
CREATE INDEX IF NOT EXISTS ncm_outbox_due ON ncm_notification_outbox(next_attempt_at) WHERE status='pending';

CREATE TABLE IF NOT EXISTS ncm_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id UUID,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    device_id UUID,
    resource_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Ordinary application updates/deletes cannot modify the audit trail. A DBA
-- still controls the database and retention; offsite evidence remains required.
CREATE OR REPLACE FUNCTION ncm_audit_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'NCM audit records are append-only'; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ncm_audit_immutable ON ncm_audit;
CREATE TRIGGER ncm_audit_immutable BEFORE UPDATE OR DELETE ON ncm_audit
FOR EACH ROW EXECUTE FUNCTION ncm_audit_append_only();
