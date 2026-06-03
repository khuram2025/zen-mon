-- migrate-025-ncm-schedule.sql
-- E4 slice 3: per-device backup schedule interval for NCM auto-backup.
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS schedule_interval_hours INTEGER NOT NULL DEFAULT 24;
