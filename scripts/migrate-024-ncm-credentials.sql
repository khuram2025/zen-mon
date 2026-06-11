-- migrate-024-ncm-credentials.sql
-- E4 slice 2: professional NCM — connection profiles (CLI credentials) and
-- per-device enrollment for real SSH config backup.

CREATE TABLE IF NOT EXISTS ncm_credentials (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(120) NOT NULL,
    description         TEXT,
    protocol            VARCHAR(10) NOT NULL DEFAULT 'ssh',   -- ssh | telnet
    port                INTEGER NOT NULL DEFAULT 22,
    username            VARCHAR(120) NOT NULL,
    password_enc        BYTEA,
    enable_password_enc BYTEA,
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS device_ncm (
    device_id        UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    credential_id    UUID REFERENCES ncm_credentials(id) ON DELETE SET NULL,
    platform         VARCHAR(40) NOT NULL DEFAULT 'autodetect',  -- netmiko device_type
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_status      VARCHAR(20),       -- success | failed
    last_error       TEXT,
    last_attempt_at  TIMESTAMPTZ,
    last_success_at  TIMESTAMPTZ
);
