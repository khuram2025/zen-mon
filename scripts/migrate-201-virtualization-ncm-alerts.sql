-- Migration 201: virtualisation alert rules and NCM alerts through the alert
-- pipeline (alerting audit 2026-11-19, gaps G-11 and G-14).
--
-- * alert_rules_metric_check gains the metrics evaluated by
--   services/virtualization_alert_service.py:
--     vm_host_disconnected  1 while vCenter reports an ESXi host disconnected
--                           or not responding, else 0 (dimension = host)
--     vm_datastore_pct      used % of a datastore / storage container
--                           (dimension = datastore)
--     vm_alarm              an active vCenter (or Prism) alarm: 2 = red /
--                           critical, 1 = yellow / warning, 0 = cleared
--                           (dimension = alarm)
--   Widened in place (migrate-143/199 style): the current definition is kept
--   verbatim and the new list is OR-ed onto it, so this file never narrows a
--   metric another subsystem added, in any order.
-- * virtualization_alert_state keeps, per rule and asset (+ dimension), when a
--   breach or a clear started, so min_duration / clear_duration holds survive
--   restarts and are shared by both API workers.
-- * ncm_alert_state tracks the rule-less NCM conditions that are levels, not
--   events: a compliance policy failing on a device (kind 'compliance',
--   subject = policy id) and running configuration not saved to startup
--   (kind 'unsaved_config'). Violations already failing when this migration
--   runs are seeded as baseline rows: their alerts open without a
--   notification, so an upgrade does not page for old findings.
--
-- alert_rules is owned by the postgres superuser, so this file is applied by a
-- superuser. Idempotent: every statement is guarded.

BEGIN;

-- ── Alert metric vocabulary ────────────────────────────────────────────────
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('vm_datastore_pct' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''vm_host_disconnected'', ''vm_datastore_pct'', '
             || '''vm_alarm''))';
    END IF;
END $$;

-- ── Virtualisation evaluation state ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS virtualization_alert_state (
    rule_id       UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    asset_key     VARCHAR(600) NOT NULL,
    breach_since  TIMESTAMPTZ NULL,
    clear_since   TIMESTAMPTZ NULL,
    last_value    DOUBLE PRECISION NULL,
    last_seen_at  TIMESTAMPTZ NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, asset_key)
);

-- Open incidents are looked up by (rule, asset key) every evaluation.
CREATE INDEX IF NOT EXISTS idx_alerts_virt_alert_key
    ON alerts (rule_id, (metadata->>'virt_alert_key'))
    WHERE metadata->>'virt_alert_key' IS NOT NULL;

COMMENT ON TABLE virtualization_alert_state IS
    'Virtualisation alert evaluator: when the breach or clear of each rule + asset (+ dimension) began.';

-- ── NCM level conditions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ncm_alert_state (
    device_id      UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('compliance', 'unsaved_config')),
    subject        TEXT NOT NULL DEFAULT '',
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_id       UUID NULL,
    baseline       BOOLEAN NOT NULL DEFAULT false,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (device_id, kind, subject)
);

COMMENT ON TABLE ncm_alert_state IS
    'Open NCM level conditions (compliance policy failing, unsaved running configuration) and the alert each one raised.';

DO $$
BEGIN
    IF to_regclass('public.ncm_compliance_results') IS NOT NULL THEN
        INSERT INTO ncm_alert_state (device_id, kind, subject, baseline)
        SELECT DISTINCT r.device_id, 'compliance', r.policy_id::text, true
          FROM ncm_compliance_results r
         WHERE r.status = 'fail'
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON virtualization_alert_state TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ncm_alert_state TO zenplus;
    END IF;
END $$;

COMMIT;
