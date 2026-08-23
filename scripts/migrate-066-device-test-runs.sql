-- Migration 066: persist the last on-demand device test per device+kind
--
-- The device page's Ping Test / SNMP Test tiles used to remember their last
-- result in the browser's localStorage only, so the tile read "Never run" for
-- every other operator, every other browser, and after any cache clear — even
-- on a device the poller has been polling over SNMP all along.
--
-- One row per (device_id, kind) — the tile only ever shows the latest run, so
-- the table stays bounded at 2 rows per device instead of growing with every
-- click. The API upserts it; the device page reads it and falls back to
-- passive poller evidence when no manual test has been run.
--
-- Idempotent and safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS device_test_runs (
    device_id  UUID        NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    -- 'ping' | 'snmp'. Free-form so a later tool (SSH, trap, NetFlow probe)
    -- can reuse the table without a schema change.
    kind       VARCHAR(20) NOT NULL,
    ok         BOOLEAN     NOT NULL,
    -- Short one-line result shown under the tile ("3/3 · 1.2 ms").
    summary    TEXT,
    -- Full probe response, so the dialog can be re-opened without re-probing.
    detail     JSONB,
    ran_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ran_by     UUID        REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (device_id, kind)
);

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON device_test_runs TO zenplus;
  END IF;
END $$;

COMMIT;
