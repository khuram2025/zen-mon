-- migrate-190-discovery-rule-engine.sql
-- Discovery rules (phase 1): ordered, global rules that classify scan results.
--
-- A rule has a condition tree over the facts a discovery scan reports
-- (sysObjectID, vendor, IP subnet, open ports, ...) and actions: the import
-- decision (auto-import / review / ignore) and the settings an imported
-- device gets (group, template, tags, location, type, SNMP credential,
-- intervals). Rules are global and ordered by priority (lower first); for each
-- single-valued setting the first matching rule wins, tags from every
-- matching rule are merged.
--
--   discovery_engine_rules          the rules; `mode` draft | simulate |
--                                   active | disabled (phase 1 uses draft,
--                                   active and disabled; simulate is reserved
--                                   for phase 2). enforcement / on_unmatch /
--                                   max_changes_per_run are reserved for the
--                                   later phases that act on existing assets.
--   discovery_engine_rule_versions  one snapshot per saved revision.
--   discovery_engine_runs           one row per evaluation (a scan, a
--                                   re-evaluation of a past scan) with counts.
--   discovery_engine_run_items      what each rule did to each result, with
--                                   the outcome (applied, simulated, shadowed,
--                                   blocked_license, blocked_cap, error).
--   discovery_results_v2            gains rule_plan (the computed plan and a
--                                   compact trace), disposition and
--                                   matched_rule_ids.
--   system_settings['discovery_rules']  engine settings; the engine starts
--                                   OFF, so an upgrade changes nothing.
--
-- The legacy discovery_rules table (migrate-017) is left in place. No code
-- path ever wrote to it, so there is nothing to copy; it is superseded.
-- Idempotent: tolerates a re-run. No existing data is changed.

CREATE TABLE IF NOT EXISTS discovery_engine_rules (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 TEXT NOT NULL,
    description          TEXT,
    priority             INTEGER NOT NULL,
    mode                 TEXT NOT NULL DEFAULT 'draft',
    asset_kinds          TEXT[] NOT NULL DEFAULT ARRAY['device']::text[],
    triggers             TEXT[] NOT NULL DEFAULT ARRAY['discovery']::text[],
    profile_ids          UUID[],
    condition            JSONB NOT NULL DEFAULT '{"op": "all", "conditions": []}'::jsonb,
    actions              JSONB NOT NULL DEFAULT '[]'::jsonb,
    enforcement          TEXT NOT NULL DEFAULT 'set_once',
    on_unmatch           TEXT NOT NULL DEFAULT 'keep',
    stop_processing      BOOLEAN NOT NULL DEFAULT false,
    max_changes_per_run  INTEGER,
    revision             INTEGER NOT NULL DEFAULT 1,
    builtin_key          TEXT,
    created_by           UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by           UUID REFERENCES users(id) ON DELETE SET NULL,
    activated_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    activated_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_evaluated_at    TIMESTAMPTZ,
    last_match_count     INTEGER,
    CONSTRAINT discovery_engine_rules_name_len CHECK (length(name) BETWEEN 1 AND 150),
    CONSTRAINT discovery_engine_rules_mode CHECK (mode IN ('draft', 'simulate', 'active', 'disabled')),
    CONSTRAINT discovery_engine_rules_enforcement CHECK (enforcement IN ('set_once', 'enforce')),
    CONSTRAINT discovery_engine_rules_on_unmatch CHECK (on_unmatch IN ('keep', 'revert'))
);

-- Priorities are unique; reordering swaps them inside one transaction.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'discovery_engine_rules_priority_key') THEN
        ALTER TABLE discovery_engine_rules
            ADD CONSTRAINT discovery_engine_rules_priority_key UNIQUE (priority)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS discovery_engine_rules_name_key
    ON discovery_engine_rules (lower(name));

CREATE TABLE IF NOT EXISTS discovery_engine_rule_versions (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     UUID NOT NULL REFERENCES discovery_engine_rules(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    snapshot    JSONB NOT NULL,
    changed_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason      TEXT,
    UNIQUE (rule_id, revision)
);

CREATE TABLE IF NOT EXISTS discovery_engine_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger           TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'apply',
    discovery_run_id  UUID REFERENCES discovery_runs(id) ON DELETE SET NULL,
    rules_revision    TEXT,
    started_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',
    counts            JSONB NOT NULL DEFAULT '{}'::jsonb,
    detail            TEXT,
    undone_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    undone_at         TIMESTAMPTZ,
    CONSTRAINT discovery_engine_runs_mode CHECK (mode IN ('apply', 'annotate', 'simulate')),
    CONSTRAINT discovery_engine_runs_status CHECK (
        status IN ('running', 'completed', 'partial', 'needs_approval', 'failed', 'undone'))
);
CREATE INDEX IF NOT EXISTS discovery_engine_runs_started
    ON discovery_engine_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS discovery_engine_runs_discovery_run
    ON discovery_engine_runs (discovery_run_id) WHERE discovery_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS discovery_engine_run_items (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               UUID NOT NULL REFERENCES discovery_engine_runs(id) ON DELETE CASCADE,
    asset_kind           TEXT NOT NULL,
    asset_id             UUID,
    discovery_result_id  BIGINT REFERENCES discovery_results_v2(id) ON DELETE SET NULL,
    ip_address           INET,
    rule_id              UUID,
    rule_name            TEXT,
    rule_revision        INTEGER,
    action_key           TEXT NOT NULL,
    item_key             TEXT,
    before               JSONB,
    after                JSONB,
    outcome              TEXT NOT NULL,
    detail               TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT discovery_engine_run_items_outcome CHECK (outcome IN (
        'applied', 'simulated', 'shadowed', 'kept_manual_edit', 'blocked_license',
        'blocked_cap', 'blocked_child', 'blocked_permission', 'not_ready', 'error', 'reverted'))
);
CREATE INDEX IF NOT EXISTS discovery_engine_run_items_run
    ON discovery_engine_run_items (run_id, id);
CREATE INDEX IF NOT EXISTS discovery_engine_run_items_rule
    ON discovery_engine_run_items (rule_id, created_at DESC) WHERE rule_id IS NOT NULL;

ALTER TABLE discovery_results_v2 ADD COLUMN IF NOT EXISTS rule_plan JSONB;
ALTER TABLE discovery_results_v2 ADD COLUMN IF NOT EXISTS disposition TEXT;
ALTER TABLE discovery_results_v2 ADD COLUMN IF NOT EXISTS matched_rule_ids UUID[];

INSERT INTO system_settings (key, value)
VALUES ('discovery_rules', '{"enabled": false, "max_auto_import_per_run": 50, "license_reserve_nodes": 0, "history_days": 180}'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE discovery_engine_rules IS
  'Global ordered discovery rules: a condition tree over scan facts and actions (import decision, group, template, tags, ...). Lower priority is evaluated first; the first match wins each single value, tags merge.';
COMMENT ON TABLE discovery_engine_runs IS
  'One evaluation of the discovery rules (a scan or a re-evaluation) with counts; items say what each rule did to each result.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            discovery_engine_rules, discovery_engine_rule_versions,
            discovery_engine_runs, discovery_engine_run_items TO zenplus;
        GRANT USAGE, SELECT ON SEQUENCE
            discovery_engine_rule_versions_id_seq, discovery_engine_run_items_id_seq TO zenplus;
    END IF;
END $$;
