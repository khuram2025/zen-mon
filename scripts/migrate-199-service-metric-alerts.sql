-- Migration 199: certificate-expiry and response-time alert rules
-- (alerting audit 2026-11-19, gap G-4).
--
-- * alert_rules_metric_check gains the metrics evaluated by
--   services/service_metric_alert_service.py:
--     service_tls_days     days until the certificate a service check saw expires
--     service_response_ms  average response time of a service check's passing runs
--     f5_cert_days         days until an F5 BIG-IP inventory certificate expires
--   The constraint is widened in place (migrate-143 style): the current
--   definition is kept verbatim and the new list is OR-ed onto it, so this file
--   never narrows a metric another subsystem added, in any order.
-- * service_metric_alert_state keeps, per rule and asset (+ dimension), when a
--   breach or a clear started, so the breach hold (min_duration) and the stable
--   recovery hold (clear_duration) survive evaluator restarts and are shared by
--   both API workers.
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
    IF old_check IS NOT NULL AND position('service_tls_days' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''service_tls_days'', ''service_response_ms'', '
             || '''f5_cert_days''))';
    END IF;
END $$;

-- ── Evaluation state ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_metric_alert_state (
    rule_id       UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    asset_key     VARCHAR(400) NOT NULL,
    breach_since  TIMESTAMPTZ NULL,
    clear_since   TIMESTAMPTZ NULL,
    last_value    DOUBLE PRECISION NULL,
    last_seen_at  TIMESTAMPTZ NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, asset_key)
);

-- Open incidents are looked up by (rule, asset key) every evaluation.
CREATE INDEX IF NOT EXISTS idx_alerts_service_metric_key
    ON alerts (rule_id, (metadata->>'service_metric_key'))
    WHERE metadata->>'service_metric_key' IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON service_metric_alert_state TO zenplus;
    END IF;
END $$;

COMMENT ON TABLE service_metric_alert_state IS
    'Certificate-expiry / response-time alert evaluator: when the breach or clear of each rule + asset (+ dimension) began.';

COMMIT;
