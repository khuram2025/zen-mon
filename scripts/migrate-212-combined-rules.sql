-- migrate-212-combined-rules.sql
-- Multi-condition alert rules, phase 2 (docs/multi-condition-rules.md).
--
-- * alert_rules_metric_check gains 'combined': a combined rule whose subject is
--   not a server (service check, device; later APM, virtualisation, appliance)
--   stores metric = 'combined', so no single-condition evaluator ever loads
--   it; its conditions live in condition_tree (migrate-211). Widened in place,
--   migrate-202 style.
-- * combined_rule_state: the breach / clear holds of a combined rule per
--   subject, plus when each condition with its own "for at least" hold became
--   true (leaf_since). Server rules move their holds here from
--   service_metric_alert_state.
-- * service_checks.server_id: the server a service check runs on, optional,
--   for conditions on a check's server. Never set automatically.
--
-- alert_rules is owned by the postgres superuser, so this file is applied by a
-- superuser. Idempotent: every statement is guarded.

BEGIN;

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('''combined''' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric = ''combined'')';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS combined_rule_state (
    rule_id       UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    subject_key   TEXT NOT NULL,
    breach_since  TIMESTAMPTZ NULL,
    clear_since   TIMESTAMPTZ NULL,
    leaf_since    JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, subject_key)
);

COMMENT ON TABLE combined_rule_state IS
    'Breach/clear holds of combined (multi-condition) alert rules per subject, and per-condition hold start times.';

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS server_id UUID NULL
    REFERENCES servers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_combined_open
    ON alerts (rule_id, (metadata->>'dedupe'))
    WHERE metadata->>'source' = 'combined_rule' AND status IN ('pending', 'active', 'acknowledged');

UPDATE alert_rules SET subject_type = 'server'
 WHERE subject_type IS NULL AND metric LIKE 'host\_%';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON combined_rule_state TO zenplus;
    END IF;
END $$;

COMMIT;
