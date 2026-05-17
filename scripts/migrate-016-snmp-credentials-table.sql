-- Migration 016: snmp_credentials table
--
-- Adds the reusable SNMP credential store used by the SNMP Profiles UI
-- (POST /api/v1/snmp-credentials). The backend route in
-- server/app/api/v1/snmp_credentials.py and the FK column added by
-- migrate-009-snmp-credential-link.sql both assume this table exists,
-- but it was never created.
--
-- Also adds the matching snmp_credential_id column on device_groups
-- (devices already got it in migration 009) and wires up the FK on both
-- so deletes go through the API's manual unlink path safely.
--
-- Idempotent and safe to re-run.

CREATE TABLE IF NOT EXISTS snmp_credentials (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 VARCHAR(255) NOT NULL,
    description          TEXT,
    snmp_version         VARCHAR(5)  NOT NULL DEFAULT '2c'
                         CHECK (snmp_version IN ('1', '2c', '3')),
    -- v1 / v2c
    community            TEXT,
    -- v3 / USM
    v3_username          VARCHAR(255),
    v3_context           VARCHAR(255),
    v3_security_level    VARCHAR(20)
                         CHECK (v3_security_level IS NULL
                                OR v3_security_level IN ('noAuthNoPriv','authNoPriv','authPriv')),
    v3_auth_protocol     VARCHAR(20),
    v3_auth_passphrase   TEXT,
    v3_priv_protocol     VARCHAR(20),
    v3_priv_passphrase   TEXT,
    -- Connection
    port                 INTEGER NOT NULL DEFAULT 161
                         CHECK (port BETWEEN 1 AND 65535),
    timeout_ms           INTEGER NOT NULL DEFAULT 2000
                         CHECK (timeout_ms BETWEEN 200 AND 30000),
    retries              INTEGER NOT NULL DEFAULT 2
                         CHECK (retries BETWEEN 0 AND 10),
    is_default           BOOLEAN NOT NULL DEFAULT FALSE,
    -- Audit
    created_by           UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one global default credential.
CREATE UNIQUE INDEX IF NOT EXISTS idx_snmp_credentials_one_default
    ON snmp_credentials ((TRUE))
    WHERE is_default;

CREATE INDEX IF NOT EXISTS idx_snmp_credentials_name
    ON snmp_credentials (name);

-- device_groups was missing the FK column (devices got it in 009).
ALTER TABLE device_groups
    ADD COLUMN IF NOT EXISTS snmp_credential_id UUID;

-- Wire up FKs from devices / device_groups -> snmp_credentials.
-- Deletes are handled in the API by unlinking first; ON DELETE SET NULL
-- gives a safe fallback if a row is removed out-of-band.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'devices_snmp_credential_id_fkey'
    ) THEN
        ALTER TABLE devices
            ADD CONSTRAINT devices_snmp_credential_id_fkey
            FOREIGN KEY (snmp_credential_id)
            REFERENCES snmp_credentials(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'device_groups_snmp_credential_id_fkey'
    ) THEN
        ALTER TABLE device_groups
            ADD CONSTRAINT device_groups_snmp_credential_id_fkey
            FOREIGN KEY (snmp_credential_id)
            REFERENCES snmp_credentials(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_devices_snmp_credential_id
    ON devices (snmp_credential_id)
    WHERE snmp_credential_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_device_groups_snmp_credential_id
    ON device_groups (snmp_credential_id)
    WHERE snmp_credential_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON snmp_credentials TO zenplus;
    END IF;
END $$;
