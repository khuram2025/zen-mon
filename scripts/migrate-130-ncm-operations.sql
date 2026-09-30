-- Migration 130: NCCM operations — diagnosable attempts, correlated incidents,
-- managed SSH trust, key-based profiles and recovery assurance.
--
-- * Attempts (ncm_backup_runs) now carry their job, failure stage/category and
--   whether the failure is retryable, so a failed job is explainable without
--   shell access and permanent setup failures stop consuming retries.
-- * ncm_incidents correlates repeated failures of one device into ONE open
--   incident (occurrence count) that closes on the next successful capture.
-- * ncm_host_keys is operator-managed SSH trust registered through an audited,
--   fingerprint-confirmed workflow. Nothing learns keys automatically; the
--   appliance trust file (/etc/zenplus/known_hosts) keeps working alongside it.
-- * Profiles gain SSH key authentication and rotation metadata.
-- * Versions record the resolved driver and artifact format; restore tests are
--   recorded separately from capture validation.
-- * ncm_change_events holds syslog/trap configuration-change evidence used to
--   trigger debounced event backups.
-- Additive and idempotent.

ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS job_id UUID;
ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS failure_category TEXT;
ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS retryable BOOLEAN;
ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS platform TEXT;
CREATE INDEX IF NOT EXISTS ncm_runs_job ON ncm_backup_runs(job_id);
CREATE INDEX IF NOT EXISTS ncm_runs_time ON ncm_backup_runs(started_at DESC);

ALTER TABLE ncm_jobs ADD COLUMN IF NOT EXISTS failure_category TEXT;
ALTER TABLE ncm_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
CREATE INDEX IF NOT EXISTS ncm_jobs_created ON ncm_jobs(created_at DESC);

ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS last_failure_category TEXT;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS event_backup BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS ncm_incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'backup_failure',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'recovered')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    occurrences INTEGER NOT NULL DEFAULT 1,
    failure_category TEXT,
    last_message TEXT,
    last_job_id UUID,
    alert_id UUID,
    recovered_at TIMESTAMPTZ,
    recovered_run_id UUID
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_incidents_open ON ncm_incidents(device_id, kind) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ncm_incidents_device_time ON ncm_incidents(device_id, opened_at DESC);

CREATE TABLE IF NOT EXISTS ncm_host_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    key_type TEXT NOT NULL,
    key_base64 TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    verification_note TEXT NOT NULL,
    registered_by UUID,
    registered_by_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at TIMESTAMPTZ,
    retired_by_name TEXT,
    retire_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_host_keys_active ON ncm_host_keys(host, port) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ncm_host_keys_device ON ncm_host_keys(device_id, created_at DESC);

ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS auth_method TEXT NOT NULL DEFAULT 'password';
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS private_key_enc BYTEA;
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS key_passphrase_enc BYTEA;
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS key_type TEXT;
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS key_fingerprint TEXT;
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS secret_rotated_at TIMESTAMPTZ;
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS rotation_days INTEGER;

ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS artifact_format TEXT;

CREATE TABLE IF NOT EXISTS ncm_restore_tests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    version_id UUID NOT NULL REFERENCES device_configs(id) ON DELETE RESTRICT,
    result TEXT NOT NULL CHECK (result IN ('passed', 'partial', 'failed')),
    method TEXT NOT NULL,
    target TEXT NOT NULL,
    scope_notes TEXT,
    notes TEXT,
    tested_at TIMESTAMPTZ NOT NULL,
    actor_id UUID,
    actor_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ncm_restore_tests_device ON ncm_restore_tests(device_id, tested_at DESC);

CREATE TABLE IF NOT EXISTS ncm_change_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('syslog', 'trap', 'api')),
    source_ref TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    summary TEXT NOT NULL,
    reported_user TEXT,
    job_id UUID,
    version_id UUID REFERENCES device_configs(id) ON DELETE SET NULL,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_change_events_ref ON ncm_change_events(source, source_ref);
CREATE INDEX IF NOT EXISTS ncm_change_events_pending ON ncm_change_events(device_id, received_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS ncm_change_events_device ON ncm_change_events(device_id, received_at DESC);

-- Section-engine preset for scheduled NCCM reports (and the previously
-- unschedulable rum_usage preset).
ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_report_type_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_report_type_check CHECK (
    report_type IN (
        'executive_summary','device_health','service_health','alert_analysis','full_report',
        'availability','performance','traffic','usage','apm_performance',
        'capacity','alerts','inventory','rum_usage','ncm_protection',
        'custom'
    )
);
