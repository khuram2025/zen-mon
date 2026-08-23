-- Migration 065: per-device UDT polling settings
--
-- SolarWinds-UDT-style control over layer-2 collection: which devices
-- UDT polls, how often, and with which SNMP credential. Absence of a
-- row means "UDT enabled, device's own SNMP settings, global cadence"
-- so existing installs keep their current behavior.
--
-- The Go poller LEFT JOINs this table in LoadSNMPDevices; the API
-- manages it under /api/v1/udt/settings. Per-PORT enablement uses the
-- pre-existing udt_port_state.monitored flag (no schema change needed).
--
-- Idempotent and safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS udt_device_settings (
    device_id          UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    -- NULL = poll with the device's own SNMP settings. Set when the
    -- bridge/Q-bridge MIBs need a different credential (e.g. a v3 user
    -- with per-VLAN context access on Cisco).
    snmp_credential_id UUID REFERENCES snmp_credentials(id) ON DELETE SET NULL,
    -- NULL = use the global UDT poll interval (system_settings key 'udt').
    poll_interval_s    INTEGER CHECK (poll_interval_s IS NULL
                                      OR poll_interval_s BETWEEN 60 AND 86400),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_udt_device_settings_credential
    ON udt_device_settings (snmp_credential_id)
    WHERE snmp_credential_id IS NOT NULL;

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON udt_device_settings TO zenplus;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO zenplus;
  END IF;
END $$;

COMMIT;
