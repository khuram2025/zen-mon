-- NCCM capture deadline and live capture progress.
--
-- capture_timeout_seconds: the whole-capture deadline (60..3600 s). NULL keeps
-- the inherited value: device override > connection profile > driver default
-- (FortiOS 900 s) > system default (240 s), so existing enrollments and
-- profiles keep their behaviour until an operator sets a value.
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS capture_timeout_seconds INTEGER;
ALTER TABLE device_ncm ADD COLUMN IF NOT EXISTS capture_timeout_seconds INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ncm_credentials_capture_timeout_range') THEN
        ALTER TABLE ncm_credentials ADD CONSTRAINT ncm_credentials_capture_timeout_range
            CHECK (capture_timeout_seconds IS NULL OR capture_timeout_seconds BETWEEN 60 AND 3600);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_ncm_capture_timeout_range') THEN
        ALTER TABLE device_ncm ADD CONSTRAINT device_ncm_capture_timeout_range
            CHECK (capture_timeout_seconds IS NULL OR capture_timeout_seconds BETWEEN 60 AND 3600);
    END IF;
END $$;

-- Progress of the running capture (bytes/lines received, pages advanced,
-- elapsed, deadline, current command), written by the worker every few
-- seconds while a job runs. Counters only: never configuration content.
ALTER TABLE ncm_jobs ADD COLUMN IF NOT EXISTS progress JSONB;
-- Final counters of each attempt, kept for diagnosing slow or truncated captures.
ALTER TABLE ncm_backup_runs ADD COLUMN IF NOT EXISTS capture_stats JSONB;
