-- Migration 215: what each remote sensor reports it can apply.
--
-- A sensor names its optional abilities in every heartbeat (first one:
-- "host_overrides", manual host addresses for service checks). The controller
-- keeps the latest list so a setting an older sensor ignores is shown as not
-- applied, instead of silently doing nothing (docs/service-check-connections.md).
-- Additive and replay-safe.
BEGIN;
ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '[]'::jsonb;
COMMIT;
