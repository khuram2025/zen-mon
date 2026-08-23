-- Migration 057: storage management (Settings -> Storage)
-- Audit trail for purge/backup/restore actions plus the appliance backup
-- catalog. Retention + auto-purge + backup-schedule settings live in
-- system_settings under keys 'storage.retention' / 'storage.backup_schedule'.

CREATE TABLE IF NOT EXISTS storage_events (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type  VARCHAR(40) NOT NULL,  -- auto_purge | manual_purge | retention_applied
                                       -- | backup_created | backup_failed | backup_deleted
                                       -- | restore_started | restore_completed | restore_failed
                                       -- | os_cleanup
    actor       VARCHAR(120),          -- username, or 'system' for the sweeper
    freed_bytes BIGINT NOT NULL DEFAULT 0,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_storage_events_created
    ON storage_events (created_at DESC);

CREATE TABLE IF NOT EXISTS storage_backups (
    id                  UUID PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          VARCHAR(120),
    kind                VARCHAR(20) NOT NULL DEFAULT 'config',   -- config | full
    status              VARCHAR(20) NOT NULL DEFAULT 'running',  -- running | completed | failed
    include_clickhouse  BOOLEAN NOT NULL DEFAULT FALSE,
    size_bytes          BIGINT NOT NULL DEFAULT 0,
    path                TEXT,
    note                TEXT,
    error               TEXT,
    finished_at         TIMESTAMPTZ,
    last_restore_at     TIMESTAMPTZ,
    last_restore_status VARCHAR(20),   -- running | completed | failed
    last_restore_error  TEXT
);

CREATE INDEX IF NOT EXISTS idx_storage_backups_created
    ON storage_backups (created_at DESC);

GRANT ALL ON storage_events, storage_backups TO zenplus;
GRANT USAGE, SELECT ON SEQUENCE storage_events_id_seq TO zenplus;
