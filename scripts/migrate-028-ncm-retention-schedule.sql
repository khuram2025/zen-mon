-- migrate-028-ncm-retention-schedule.sql
-- E4: configurable version retention + flexible scheduling for NCM.
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS keep_versions INTEGER NOT NULL DEFAULT 5;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS schedule_type VARCHAR(10) NOT NULL DEFAULT 'interval'; -- interval | daily | weekly
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS schedule_time TIME;        -- for daily/weekly (server local time)
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS schedule_days INTEGER[];   -- weekly: 0=Sun .. 6=Sat (Postgres DOW)
