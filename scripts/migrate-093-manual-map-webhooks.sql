-- Manual-map webhook fan-out fields used by alert_engine.
--
-- The runtime query shipped before these columns had a migration.  Appliances
-- could therefore report a clean migration ledger while every device status
-- transition logged UndefinedColumn and skipped map webhook delivery.

ALTER TABLE manual_maps
    ADD COLUMN IF NOT EXISTS webhook_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE manual_maps
    ADD COLUMN IF NOT EXISTS webhook_url TEXT;

COMMENT ON COLUMN manual_maps.webhook_enabled IS
    'Send device status transitions to this map webhook when enabled.';

COMMENT ON COLUMN manual_maps.webhook_url IS
    'HTTPS endpoint for map-scoped device status transition notifications.';
