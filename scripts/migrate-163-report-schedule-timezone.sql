-- Migration 163: scheduled reports carry their own IANA timezone.
--
-- A schedule stored only an hour and minute, read in whatever the organisation
-- timezone was at run time. Field report (October 2026): daily reports titled
-- "08:00 Riyadh" ran at 05:00 Riyadh (02:00 UTC) because the time had been
-- entered already converted to UTC and was then read as Asia/Riyadh.
--
-- The backfill pins every existing schedule to the organisation timezone it
-- runs in today, so no schedule moves on upgrade; operators correct a
-- mis-entered hour themselves (the UI now shows local time and UTC).
-- Additive and idempotent.

BEGIN;

ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS timezone VARCHAR(64);

UPDATE report_schedules
   SET timezone = COALESCE(
           NULLIF(btrim((SELECT value->>'timezone' FROM system_settings WHERE key = 'company')), ''),
           'UTC')
 WHERE timezone IS NULL;

COMMENT ON COLUMN report_schedules.timezone IS
    'IANA timezone the schedule hour/minute are expressed in (e.g. Asia/Riyadh).';

COMMIT;
