-- Migration 140: NetFlow detector exceptions get an exporter scope
-- (NetFlow development review, wave C polish).
--
-- * netflow_detector_exceptions.exporter_ips: the flow exporters an exception
--   applies to. NULL = every exporter (a global exception, only an
--   unrestricted netflow.manage user creates one); otherwise the exception
--   matches findings from these exporters only. A tag-scoped manager's
--   exception is bound at creation to the exporters that manager may see, so
--   it can never suppress findings on another team's exporters; the API never
--   widens the list afterwards.
-- * The list, when present, is non-empty and has no NULL element (an empty
--   scope would be an exception that silently matches nothing).
--
-- The table is created by migrate-138 (same owner). Idempotent: every
-- statement is guarded, nothing is written to existing rows (they stay
-- global, which is what they did before this column existed).

BEGIN;

ALTER TABLE netflow_detector_exceptions ADD COLUMN IF NOT EXISTS exporter_ips INET[] NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'netflow_detector_exceptions'::regclass
           AND conname = 'netflow_detector_exceptions_exporter_ips_check'
    ) THEN
        ALTER TABLE netflow_detector_exceptions
            ADD CONSTRAINT netflow_detector_exceptions_exporter_ips_check
            CHECK (exporter_ips IS NULL
                   OR (cardinality(exporter_ips) > 0 AND array_position(exporter_ips, NULL) IS NULL));
    END IF;
END $$;

COMMENT ON COLUMN netflow_detector_exceptions.exporter_ips IS
    'Exporters the exception applies to; NULL = every exporter. A tag-scoped creator''s exception is bound to the exporters visible to that creator.';

COMMIT;
