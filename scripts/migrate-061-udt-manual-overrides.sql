-- migrate-061-udt-manual-overrides.sql
-- Make operator edits to an endpoint's authorized / ignored flags sticky.
--
-- The UDT sweeper recomputes authorized (from allow rules) and ignored
-- (from ignore rules) every pass. Without an override marker it would
-- clobber a manual "Mark allowed" / "Flag rogue" / "Ignore" within 60s
-- and re-emit rogue events. These columns record operator intent so the
-- sweeper only auto-manages endpoints the operator hasn't pinned.
--
--   authorized_override  NULL = rule-driven; TRUE/FALSE = manual, sticky
--   ignored_manual       TRUE = manually ignored, never auto-cleared

BEGIN;

ALTER TABLE udt_endpoints ADD COLUMN IF NOT EXISTS authorized_override BOOLEAN;
ALTER TABLE udt_endpoints ADD COLUMN IF NOT EXISTS ignored_manual BOOLEAN NOT NULL DEFAULT FALSE;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON udt_endpoints TO zenplus;
  END IF;
END $$;

COMMIT;
