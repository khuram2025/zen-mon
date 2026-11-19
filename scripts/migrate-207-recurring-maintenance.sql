-- Migration 207: recurring maintenance windows and server scope (alerting
-- audit 2026-11-19, plan step 2.7, gap G-6; docs/maintenance-windows.md).
--
-- device_maintenance (migrate-053) held one-off windows for devices only.
--
-- * Recurrence: a window may repeat weekly on chosen days (ISO 1 = Monday ..
--   7 = Sunday) at a local start time for a number of minutes, in an IANA time
--   zone, optionally until a local date (inclusive: the last day an
--   occurrence may start). One-off windows keep recur_days NULL and behave
--   exactly as before.
--   For a recurring window starts_at / ends_at hold its FIRST occurrence
--   (written by the API). Occurrences before starts_at never count. A reader
--   that does not know about recurrence therefore sees the first occurrence
--   only and never an endless window: it fails towards alerting, not towards
--   silence.
-- * Server scope: 'server' (scope_server_id), 'server_group' (a device group
--   that contains servers; subgroups roll up through
--   device_group_member_closure) and 'server_tag' (servers.tags). The existing
--   'device', 'group', 'tag' and 'all' scopes still cover devices only.
-- * device_maintenance_occurrences(m, from, to) is the one definition of
--   "when is this window in force": the occurrences (one-off: the window
--   itself) that overlap [from, to], bounds inclusive as before. Every
--   evaluator, the Go poller, the sensor API and the reports use it, through
--   device_maintenance_active(m, at) for "is it in force now".
--   Local times are converted with PostgreSQL's time zone rules; the duration
--   is absolute minutes, so an occurrence that crosses a DST change still
--   lasts exactly that long. A local start time that does not exist on the
--   day the clocks go forward is read as PostgreSQL reads it (the offset in
--   force before the change); app.services.maintenance_windows mirrors this.
--
-- Idempotent: every statement is guarded; a re-run changes nothing.

BEGIN;

ALTER TABLE device_maintenance
    ADD COLUMN IF NOT EXISTS scope_server_id UUID REFERENCES servers(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS recur_days SMALLINT[],
    ADD COLUMN IF NOT EXISTS recur_start_time TIME,
    ADD COLUMN IF NOT EXISTS recur_duration_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS recur_timezone VARCHAR(64),
    ADD COLUMN IF NOT EXISTS recur_until DATE;

DO $$
DECLARE
    def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO def FROM pg_constraint
     WHERE conrelid = 'device_maintenance'::regclass AND conname = 'device_maintenance_scope_type_check';
    IF def IS NULL OR position('server_tag' IN def) = 0 THEN
        ALTER TABLE device_maintenance DROP CONSTRAINT IF EXISTS device_maintenance_scope_type_check;
        ALTER TABLE device_maintenance ADD CONSTRAINT device_maintenance_scope_type_check
            CHECK (scope_type IN ('device', 'group', 'tag', 'all', 'server', 'server_group', 'server_tag'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_maintenance_server_scope') THEN
        -- Only the server itself: a deleted tag clears scope_tag (the window
        -- then matches nothing, as a device tag window does) and a deleted
        -- group deletes its windows (ON DELETE CASCADE).
        ALTER TABLE device_maintenance ADD CONSTRAINT device_maintenance_server_scope CHECK (
            scope_type <> 'server' OR scope_server_id IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_maintenance_recurrence') THEN
        ALTER TABLE device_maintenance ADD CONSTRAINT device_maintenance_recurrence CHECK (
            (recur_days IS NULL AND recur_start_time IS NULL AND recur_duration_minutes IS NULL
             AND recur_timezone IS NULL AND recur_until IS NULL)
            OR (recur_days IS NOT NULL AND cardinality(recur_days) BETWEEN 1 AND 7
                AND recur_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
                AND recur_start_time IS NOT NULL
                AND recur_duration_minutes BETWEEN 1 AND 10080
                AND recur_timezone IS NOT NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_dm_server ON device_maintenance (scope_server_id)
    WHERE scope_server_id IS NOT NULL;

-- A zone PostgreSQL does not know would make every maintenance query fail
-- (the poller's included), so it is refused when the row is written. A
-- trigger, not a CHECK: zone names come from the operating system's data.
CREATE OR REPLACE FUNCTION device_maintenance_check_timezone() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.recur_timezone IS NOT NULL THEN
        BEGIN
            PERFORM now() AT TIME ZONE NEW.recur_timezone;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'unknown time zone "%"', NEW.recur_timezone
                USING ERRCODE = 'check_violation';
        END;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS device_maintenance_timezone ON device_maintenance;
CREATE TRIGGER device_maintenance_timezone
    BEFORE INSERT OR UPDATE OF recur_timezone ON device_maintenance
    FOR EACH ROW EXECUTE FUNCTION device_maintenance_check_timezone();

-- The occurrences of one window that overlap [p_from, p_to] (inclusive).
-- Recurring: every local day from p_from's day minus the longest possible
-- reach (duration in days, plus one for a DST-shortened day) to p_to's day.
CREATE OR REPLACE FUNCTION device_maintenance_occurrences(
    p_starts TIMESTAMPTZ, p_ends TIMESTAMPTZ, p_days SMALLINT[], p_start_time TIME,
    p_minutes INTEGER, p_tz TEXT, p_until DATE, p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS TABLE (occ_start TIMESTAMPTZ, occ_end TIMESTAMPTZ)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT p_starts, p_ends
     WHERE p_days IS NULL AND p_starts <= p_to AND p_ends >= p_from
    UNION ALL
    SELECT o.s, o.s + make_interval(mins => p_minutes)
      FROM (SELECT (p_from AT TIME ZONE p_tz)::date - ((p_minutes + 1439) / 1440) - 1 AS first_day,
                   (p_to AT TIME ZONE p_tz)::date AS last_day
             WHERE p_days IS NOT NULL) b
     CROSS JOIN LATERAL generate_series(0, b.last_day - b.first_day) AS k
     CROSS JOIN LATERAL (SELECT b.first_day + k AS day) d
     CROSS JOIN LATERAL (SELECT (d.day + p_start_time) AT TIME ZONE p_tz AS s) o
     WHERE extract(isodow FROM d.day)::int = ANY (p_days)
       AND (p_until IS NULL OR d.day <= p_until)
       AND o.s >= p_starts
       AND o.s <= p_to
       AND o.s + make_interval(mins => p_minutes) >= p_from
$$;

CREATE OR REPLACE FUNCTION device_maintenance_occurrences(
    m device_maintenance, p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS TABLE (occ_start TIMESTAMPTZ, occ_end TIMESTAMPTZ)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT * FROM device_maintenance_occurrences(
        m.starts_at, m.ends_at, m.recur_days, m.recur_start_time, m.recur_duration_minutes,
        m.recur_timezone, m.recur_until, p_from, p_to)
$$;

CREATE OR REPLACE FUNCTION device_maintenance_active(m device_maintenance, p_at TIMESTAMPTZ)
RETURNS BOOLEAN
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT EXISTS (SELECT 1 FROM device_maintenance_occurrences(m, p_at, p_at))
$$;

COMMENT ON COLUMN device_maintenance.recur_days IS
    'Recurring window: ISO weekdays (1 = Monday .. 7 = Sunday) an occurrence starts on; NULL = one-off.';
COMMENT ON COLUMN device_maintenance.recur_until IS
    'Recurring window: last local date an occurrence may start (inclusive); NULL = no end.';
COMMENT ON FUNCTION device_maintenance_active(device_maintenance, TIMESTAMPTZ) IS
    'Whether a maintenance window (one-off or recurring) is in force at the instant. Every evaluator uses this.';

COMMIT;
