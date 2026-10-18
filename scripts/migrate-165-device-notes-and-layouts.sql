-- Migration 165: device notes and owner, and server-saved device page layouts.
--
-- Device detail page, Phase 3 (docs/device-detail-page.md):
--
--  * devices.owner / devices.notes: who looks after the device and the
--    operational notes an operator needs on the page (runbook pointers,
--    circuit IDs, "reboot only after 22:00"). Free text, edited with
--    devices.manage; NULL = none recorded. devices.description is unchanged.
--  * device_widget_layouts: a user's widget layout for one device page tab
--    (scope 'device', keyed by device_id + section) or for every device of one
--    vendor and device type (scope 'template', keyed by the normalised vendor
--    + device type + section). Layouts used to live only in browser storage,
--    so they did not follow the user to another browser. The scoping is the
--    one the browser keys already used: per user, per device or vendor/type,
--    per tab; a device layout wins over the vendor/type template. ``items``
--    keeps entries for widgets that are temporarily absent.
--
-- Additive and idempotent; one transaction so the grant cannot be left behind.

BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS owner VARCHAR(255);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS notes TEXT;
COMMENT ON COLUMN devices.owner IS 'Person or team responsible for the device (free text).';
COMMENT ON COLUMN devices.notes IS 'Operational notes shown on the device page (free text).';

CREATE TABLE IF NOT EXISTS device_widget_layouts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope       VARCHAR(16) NOT NULL CHECK (scope IN ('device', 'template')),
    device_id   UUID REFERENCES devices(id) ON DELETE CASCADE,
    vendor      VARCHAR(200),
    device_type VARCHAR(100),
    section     VARCHAR(80) NOT NULL,
    items       JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((scope = 'device' AND device_id IS NOT NULL AND vendor IS NULL AND device_type IS NULL)
        OR (scope = 'template' AND device_id IS NULL AND vendor IS NOT NULL AND device_type IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_device_widget_layouts_device
    ON device_widget_layouts (user_id, device_id, section) WHERE scope = 'device';
CREATE UNIQUE INDEX IF NOT EXISTS uq_device_widget_layouts_template
    ON device_widget_layouts (user_id, vendor, device_type, section) WHERE scope = 'template';
CREATE INDEX IF NOT EXISTS idx_device_widget_layouts_device
    ON device_widget_layouts (device_id) WHERE device_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_widget_layouts TO zenplus;
    END IF;
END $$;

COMMIT;
