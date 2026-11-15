-- migrate-187-alert-rule-exclusions.sql
-- Per-asset exclusions from shared alert rules.
--
-- A rule that applies to many devices or servers (no device/server of its
-- own: "Server restart", "Device Down", a group/tag/type rule) can be turned
-- off for one asset without disabling it for everyone. Each row says "rule R
-- does not evaluate asset A": every evaluator that matches rules to devices
-- or servers skips the pair, the rule keeps running for every other asset.
--
--   asset_kind     'device' (device_id set) or 'server' (server_id set);
--                  controller-managed children are devices.
--   reason         optional operator note.
--   created_by     who excluded it (name kept for when the user is deleted).
--
-- Deleting the rule, the device or the server deletes its exclusions.
-- Idempotent: tolerates a re-run. No existing data is changed.

CREATE TABLE IF NOT EXISTS alert_rule_exclusions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    asset_kind      TEXT NOT NULL,
    device_id       UUID REFERENCES devices(id) ON DELETE CASCADE,
    server_id       UUID REFERENCES servers(id) ON DELETE CASCADE,
    reason          TEXT,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_by_name TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT alert_rule_exclusions_asset CHECK (
        (asset_kind = 'device' AND device_id IS NOT NULL AND server_id IS NULL)
     OR (asset_kind = 'server' AND server_id IS NOT NULL AND device_id IS NULL)
    ),
    CONSTRAINT alert_rule_exclusions_reason_len CHECK (reason IS NULL OR length(reason) <= 500)
);

CREATE UNIQUE INDEX IF NOT EXISTS alert_rule_exclusions_rule_device
    ON alert_rule_exclusions (rule_id, device_id) WHERE device_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS alert_rule_exclusions_rule_server
    ON alert_rule_exclusions (rule_id, server_id) WHERE server_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS alert_rule_exclusions_device
    ON alert_rule_exclusions (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS alert_rule_exclusions_server
    ON alert_rule_exclusions (server_id) WHERE server_id IS NOT NULL;

COMMENT ON TABLE alert_rule_exclusions IS
  'Assets a shared alert rule does not evaluate (disabled for this device/server only). Every rule evaluator skips the pair; the rule keeps running for every other asset.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON alert_rule_exclusions TO zenplus;
    END IF;
END $$;
