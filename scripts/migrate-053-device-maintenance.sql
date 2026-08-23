-- migrate-053-device-maintenance.sql
--
-- Device maintenance windows: planned-downtime scheduling for devices,
-- mirroring service_check_maintenance (migrate-006). While a window is
-- active the poller keeps collecting metrics but suppresses status
-- transitions and alerting, the device shows status 'maintenance'
-- (already allowed by the devices.status CHECK), and SLA/uptime and
-- report calculations exclude samples inside the window.
--
-- Scope semantics (same as service checks): a window can target a single
-- device, a device group, a tag (devices.tags is a JSONB string array),
-- or the whole fleet.

CREATE TABLE IF NOT EXISTS device_maintenance (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type      VARCHAR(20) NOT NULL CHECK (scope_type IN ('device','group','tag','all')),
  scope_device_id UUID REFERENCES devices(id) ON DELETE CASCADE,
  scope_group_id  UUID REFERENCES device_groups(id) ON DELETE CASCADE,
  scope_tag       VARCHAR(120),
  starts_at       TIMESTAMPTZ NOT NULL,
  ends_at         TIMESTAMPTZ NOT NULL,
  reason          TEXT,
  created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);

-- The poller and the API both ask "which windows are active now / overlap
-- this range" — index the time span; scope_device_id serves the per-device
-- panel on the device detail page.
CREATE INDEX IF NOT EXISTS idx_dm_active ON device_maintenance(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_dm_device ON device_maintenance(scope_device_id);
