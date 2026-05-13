-- Migration 013: notification gateway records
-- Adds the SMTP/SMS gateway table used by notification channels and links
-- channels to an optional gateway. Idempotent and safe to re-run.

CREATE TABLE IF NOT EXISTS notification_gateways (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    type        VARCHAR(50) NOT NULL CHECK (type IN ('smtp', 'sms')),
    config      JSONB NOT NULL DEFAULT '{}',
    is_default  BOOLEAN NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE notification_channels
    ADD COLUMN IF NOT EXISTS gateway_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'notification_channels_gateway_id_fkey'
    ) THEN
        ALTER TABLE notification_channels
            ADD CONSTRAINT notification_channels_gateway_id_fkey
            FOREIGN KEY (gateway_id)
            REFERENCES notification_gateways(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_notification_channels_gateway
    ON notification_channels(gateway_id);

CREATE INDEX IF NOT EXISTS idx_notification_gateways_type
    ON notification_gateways(type);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_gateways_default_per_type
    ON notification_gateways(type)
    WHERE is_default;

INSERT INTO notification_gateways (name, type, config, is_default, enabled, created_at, updated_at)
SELECT
    'Default SMTP',
    'smtp',
    value,
    TRUE,
    COALESCE((value->>'enabled')::BOOLEAN, FALSE),
    NOW(),
    NOW()
FROM system_settings
WHERE key = 'smtp'
ON CONFLICT (type) WHERE is_default
DO UPDATE SET
    config = EXCLUDED.config,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

INSERT INTO notification_gateways (name, type, config, is_default, enabled, created_at, updated_at)
SELECT
    'Default SMS',
    'sms',
    value,
    TRUE,
    COALESCE((value->>'enabled')::BOOLEAN, FALSE),
    NOW(),
    NOW()
FROM system_settings
WHERE key = 'sms'
ON CONFLICT (type) WHERE is_default
DO UPDATE SET
    config = EXCLUDED.config,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON notification_gateways TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON notification_channels TO zenplus;
    END IF;
END $$;
