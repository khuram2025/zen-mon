-- migrate-023-ncm.sql
-- E4: Network Configuration Management (NCM) — versioned device config storage.
-- Slice 1 stores config snapshots (manual / API capture); SSH auto-fetch is a
-- later slice. Versions are de-duplicated by content hash so an unchanged
-- capture does not create a new row.

CREATE TABLE IF NOT EXISTS device_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    config_type   VARCHAR(20) NOT NULL DEFAULT 'running',   -- running | startup
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    line_count    INTEGER NOT NULL DEFAULT 0,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    captured_by   VARCHAR(40) NOT NULL DEFAULT 'manual',    -- manual | api | ssh
    source_note   TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_configs_device_time
    ON device_configs (device_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_configs_hash
    ON device_configs (device_id, content_hash);
