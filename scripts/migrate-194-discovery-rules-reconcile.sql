-- migrate-194-discovery-rules-reconcile.sql
-- Discovery rules phase 3: re-evaluation when assets change, revert on
-- un-match, change caps with approval, controller-managed children.
--
--   discovery_engine_asset_state  gains facts_hash: an md5 of the facts rules
--                                 test (status excluded, so up/down flips never
--                                 re-evaluate). The "changed" pass re-evaluates
--                                 an asset whose hash or whose rules revision
--                                 differs from the stored one.
--   discovery_engine_runs         gains plan (the changes a run that exceeded
--                                 the change cap is waiting to apply),
--                                 approved_by / approved_at; status adds
--                                 'rejected'.
--   asset kinds                   'child' (controller-managed FortiSwitch /
--                                 FortiAP) joins 'device' and 'server' in
--                                 assignments and asset state.
--
-- Rules gain the trigger value 'changed' and use on_unmatch (keep / revert),
-- both columns already exist (migrate-190). Idempotent; no data is changed.

ALTER TABLE discovery_engine_asset_state ADD COLUMN IF NOT EXISTS facts_hash TEXT;

ALTER TABLE discovery_engine_asset_state DROP CONSTRAINT IF EXISTS discovery_engine_asset_state_kind;
ALTER TABLE discovery_engine_asset_state ADD CONSTRAINT discovery_engine_asset_state_kind
    CHECK (asset_kind IN ('device', 'server', 'child'));
ALTER TABLE discovery_engine_assignments DROP CONSTRAINT IF EXISTS discovery_engine_assignments_kind;
ALTER TABLE discovery_engine_assignments ADD CONSTRAINT discovery_engine_assignments_kind
    CHECK (asset_kind IN ('device', 'server', 'child'));

ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS plan JSONB;
ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE discovery_engine_runs DROP CONSTRAINT IF EXISTS discovery_engine_runs_status;
ALTER TABLE discovery_engine_runs ADD CONSTRAINT discovery_engine_runs_status CHECK (
    status IN ('running', 'completed', 'partial', 'needs_approval', 'failed', 'undone', 'rejected'));
CREATE INDEX IF NOT EXISTS discovery_engine_runs_waiting
    ON discovery_engine_runs (started_at) WHERE status = 'needs_approval';

COMMENT ON COLUMN discovery_engine_asset_state.facts_hash IS
  'md5 of the facts discovery rules test (status excluded); the "changed" pass re-evaluates an asset when it differs.';
COMMENT ON COLUMN discovery_engine_runs.plan IS
  'Changes an automatic run held back because they exceeded the change cap; applied (re-checked value by value) on approval.';
