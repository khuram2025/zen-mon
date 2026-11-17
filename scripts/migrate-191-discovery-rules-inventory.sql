-- migrate-191-discovery-rules-inventory.sql
-- Discovery rules phase 2: rules on devices and servers already in inventory.
--
--   discovery_engine_assignments  every value a rule set on an asset, with the
--                                 value it replaced (`previous`, the value
--                                 before any rule touched it). A value that no
--                                 longer equals `value` was edited by hand:
--                                 set-once rules keep that edit, enforce rules
--                                 put their value back. Undo restores
--                                 `previous`. item_key is '' for single values
--                                 and the lower-cased tag for tags.
--   discovery_engine_asset_state  assets the "when added" sweep has handled, so
--                                 each new device or server is evaluated once.
--                                 Assets created before this migration are not
--                                 swept (created_since below).
--   discovery_engine_runs         gains targets (the filter a manual run used),
--                                 preview_digest (the preview it applied) and
--                                 undo_of (the run an undo reverted).
--   discovery_engine_run_items    more outcomes: unchanged, skipped_changed,
--                                 not_reversible; an index by asset.
--   system_settings['discovery_rules_state']  {created_since}: the sweep only
--                                 looks at assets created after the upgrade.
--
-- Assignments have no foreign key (an asset is a device or a server); the sweep
-- deletes rows whose asset is gone. Idempotent: tolerates a re-run. No existing
-- data is changed.

CREATE TABLE IF NOT EXISTS discovery_engine_assignments (
    asset_kind     TEXT NOT NULL,
    asset_id       UUID NOT NULL,
    action_key     TEXT NOT NULL,
    item_key       TEXT NOT NULL DEFAULT '',
    value          JSONB,
    previous       JSONB,
    rule_id        UUID,
    rule_name      TEXT,
    rule_revision  INTEGER,
    run_id         UUID REFERENCES discovery_engine_runs(id) ON DELETE SET NULL,
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_kind, asset_id, action_key, item_key),
    CONSTRAINT discovery_engine_assignments_kind CHECK (asset_kind IN ('device', 'server'))
);
CREATE INDEX IF NOT EXISTS discovery_engine_assignments_rule
    ON discovery_engine_assignments (rule_id) WHERE rule_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS discovery_engine_asset_state (
    asset_kind            TEXT NOT NULL,
    asset_id              UUID NOT NULL,
    created_evaluated_at  TIMESTAMPTZ,
    evaluated_at          TIMESTAMPTZ,
    rules_revision        TEXT,
    matched_rule_ids      UUID[],
    PRIMARY KEY (asset_kind, asset_id),
    CONSTRAINT discovery_engine_asset_state_kind CHECK (asset_kind IN ('device', 'server'))
);

ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS targets JSONB;
ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS preview_digest TEXT;
ALTER TABLE discovery_engine_runs ADD COLUMN IF NOT EXISTS undo_of UUID
    REFERENCES discovery_engine_runs(id) ON DELETE SET NULL;

ALTER TABLE discovery_engine_run_items DROP CONSTRAINT IF EXISTS discovery_engine_run_items_outcome;
ALTER TABLE discovery_engine_run_items ADD CONSTRAINT discovery_engine_run_items_outcome CHECK (outcome IN (
    'applied', 'simulated', 'shadowed', 'kept_manual_edit', 'blocked_license', 'blocked_cap',
    'blocked_child', 'blocked_permission', 'not_ready', 'error', 'reverted',
    'unchanged', 'skipped_changed', 'not_reversible'));
CREATE INDEX IF NOT EXISTS discovery_engine_run_items_asset
    ON discovery_engine_run_items (asset_kind, asset_id, created_at DESC) WHERE asset_id IS NOT NULL;

INSERT INTO system_settings (key, value)
VALUES ('discovery_rules_state', jsonb_build_object('created_since', now()))
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE discovery_engine_assignments IS
  'Values discovery rules set on devices/servers, with the value they replaced. A current value that differs from `value` was edited by hand (kept by set-once rules, restored by enforce rules); undo restores `previous`.';
COMMENT ON TABLE discovery_engine_asset_state IS
  'Devices/servers the discovery rules "when added" sweep has handled (each new asset is evaluated once).';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            discovery_engine_assignments, discovery_engine_asset_state TO zenplus;
    END IF;
END $$;
