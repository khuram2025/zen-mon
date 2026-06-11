-- migrate-026-ncm-change-alert.sql
-- E4: NCM config-change alerting. Per-device toggle (default on) to raise an
-- alert when a device's running-config changes between backups.
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS alert_on_change BOOLEAN NOT NULL DEFAULT TRUE;
