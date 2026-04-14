-- Migration 004: Advanced SNMP monitoring
-- Extends devices with SNMPv3 + advanced polling fields,
-- adds profile/inventory/interface/sensor tables and credential audit log.
-- Safe to re-run.

-- ─── Extend devices ───
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS snmp_v3_username     VARCHAR(255),
    ADD COLUMN IF NOT EXISTS snmp_v3_context      VARCHAR(255),
    ADD COLUMN IF NOT EXISTS snmp_auth_protocol   VARCHAR(16),
    ADD COLUMN IF NOT EXISTS snmp_auth_passphrase BYTEA,
    ADD COLUMN IF NOT EXISTS snmp_priv_protocol   VARCHAR(16),
    ADD COLUMN IF NOT EXISTS snmp_priv_passphrase BYTEA,
    ADD COLUMN IF NOT EXISTS snmp_timeout_ms      INTEGER DEFAULT 2000,
    ADD COLUMN IF NOT EXISTS snmp_retries         INTEGER DEFAULT 2,
    ADD COLUMN IF NOT EXISTS snmp_max_repetitions INTEGER DEFAULT 25,
    ADD COLUMN IF NOT EXISTS snmp_poll_interval   INTEGER DEFAULT 60,
    ADD COLUMN IF NOT EXISTS sys_object_id        VARCHAR(255),
    ADD COLUMN IF NOT EXISTS vendor               VARCHAR(100),
    ADD COLUMN IF NOT EXISTS model                VARCHAR(255),
    ADD COLUMN IF NOT EXISTS os_version           VARCHAR(255),
    ADD COLUMN IF NOT EXISTS profile_id           UUID;

-- Enum-ish checks (loose — allow unknown until v3 is fully wired)
ALTER TABLE devices
    DROP CONSTRAINT IF EXISTS devices_snmp_auth_protocol_check;
ALTER TABLE devices
    ADD CONSTRAINT devices_snmp_auth_protocol_check
    CHECK (snmp_auth_protocol IS NULL OR snmp_auth_protocol IN
        ('MD5','SHA','SHA224','SHA256','SHA384','SHA512'));

ALTER TABLE devices
    DROP CONSTRAINT IF EXISTS devices_snmp_priv_protocol_check;
ALTER TABLE devices
    ADD CONSTRAINT devices_snmp_priv_protocol_check
    CHECK (snmp_priv_protocol IS NULL OR snmp_priv_protocol IN
        ('DES','3DES','AES','AES128','AES192','AES256'));

CREATE INDEX IF NOT EXISTS idx_devices_snmp_enabled
    ON devices(snmp_enabled) WHERE snmp_enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_devices_vendor ON devices(vendor);
CREATE INDEX IF NOT EXISTS idx_devices_sys_object_id ON devices(sys_object_id);

-- ─── Device monitoring profiles (vendor packs) ───
CREATE TABLE IF NOT EXISTS device_profiles (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(255) NOT NULL,
    vendor       VARCHAR(100),
    match_rules  JSONB NOT NULL DEFAULT '{}'::jsonb,
    oid_groups   JSONB NOT NULL DEFAULT '[]'::jsonb,
    version      INTEGER NOT NULL DEFAULT 1,
    builtin      BOOLEAN NOT NULL DEFAULT FALSE,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_device_profiles_vendor ON device_profiles(vendor);
CREATE INDEX IF NOT EXISTS idx_device_profiles_builtin ON device_profiles(builtin);

-- Now that device_profiles exists, link it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'devices_profile_id_fkey'
    ) THEN
        ALTER TABLE devices
            ADD CONSTRAINT devices_profile_id_fkey
            FOREIGN KEY (profile_id) REFERENCES device_profiles(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ─── Discovered interfaces (IF-MIB) ───
CREATE TABLE IF NOT EXISTS device_interfaces (
    id           BIGSERIAL PRIMARY KEY,
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_index     INTEGER NOT NULL,
    if_name      VARCHAR(255),
    if_descr     VARCHAR(255),
    if_alias     VARCHAR(255),
    if_type      INTEGER,
    if_speed     BIGINT,
    mac_address  MACADDR,
    admin_status VARCHAR(20),
    oper_status  VARCHAR(20),
    monitored    BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, if_index)
);

CREATE INDEX IF NOT EXISTS idx_device_interfaces_device ON device_interfaces(device_id);
CREATE INDEX IF NOT EXISTS idx_device_interfaces_monitored
    ON device_interfaces(device_id) WHERE monitored = TRUE;

-- ─── Hardware inventory (ENTITY-MIB) ───
CREATE TABLE IF NOT EXISTS device_entities (
    id            BIGSERIAL PRIMARY KEY,
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ent_index     INTEGER NOT NULL,
    parent_index  INTEGER,
    class         VARCHAR(32),
    name          VARCHAR(255),
    serial_number VARCHAR(255),
    model_name    VARCHAR(255),
    hw_revision   VARCHAR(64),
    fw_revision   VARCHAR(64),
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, ent_index)
);

CREATE INDEX IF NOT EXISTS idx_device_entities_device ON device_entities(device_id);

-- ─── Sensors (ENTITY-SENSOR-MIB) ───
CREATE TABLE IF NOT EXISTS device_sensors (
    id           BIGSERIAL PRIMARY KEY,
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    sensor_index INTEGER NOT NULL,
    sensor_type  VARCHAR(32),
    description  VARCHAR(255),
    unit         VARCHAR(32),
    monitored    BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, sensor_index)
);

CREATE INDEX IF NOT EXISTS idx_device_sensors_device ON device_sensors(device_id);

-- ─── SNMP credential audit log ───
CREATE TABLE IF NOT EXISTS snmp_credential_audit (
    id         BIGSERIAL PRIMARY KEY,
    device_id  UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    actor_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    action     VARCHAR(32) NOT NULL
               CHECK (action IN ('create','rotate','delete','test','update')),
    detail     TEXT,
    client_ip  INET,
    at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snmp_credential_audit_device
    ON snmp_credential_audit(device_id, at DESC);

-- Grant write access to the application role. init-postgres.sql runs
-- as `zenplus`, but this migration is typically applied by an admin
-- (`sudo -u postgres`), which creates the new tables owned by postgres.
-- Without an explicit GRANT the poller (running as `zenplus`) gets
-- SQLSTATE 42501 on every insert.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT ALL ON device_profiles, device_interfaces, device_entities,
                     device_sensors, snmp_credential_audit TO zenplus;
        GRANT USAGE, SELECT, UPDATE ON SEQUENCE
            device_interfaces_id_seq,
            device_entities_id_seq,
            device_sensors_id_seq,
            snmp_credential_audit_id_seq
            TO zenplus;
    END IF;
END $$;

-- Extend alert_rules.metric enum to include SNMP metrics.
-- Old CHECK is replaced with an expanded list.
ALTER TABLE alert_rules
    DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
ALTER TABLE alert_rules
    ADD CONSTRAINT alert_rules_metric_check CHECK (metric IN (
        'ping_status','rtt','packet_loss','jitter',
        'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
        'if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status',
        'session_count','vpn_tunnel_state','ha_state'
    ));
