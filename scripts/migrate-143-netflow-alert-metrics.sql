-- Migration 143: NetFlow alert rules and the NetFlow management report
-- (NetFlow development review, CR-07 / FE-07 / NF-23).
--
-- * alert_rules_metric_check gains the flow metrics evaluated by
--   services/netflow_alert_service.py:
--     netflow_traffic_bps         traffic matching a FlowFilter scope (bits/s)
--     netflow_endpoint_bytes      one host moving more than N bytes in a window
--     netflow_exporter_silent     an exporter stopped sending flow records (s)
--     netflow_interface_util_pct  per-direction interface utilisation from flows
--     netflow_finding             persistent detection findings -> platform alerts
--   The constraint is widened in place (migrate-115 style): the current
--   definition is kept verbatim and the NetFlow list is OR-ed onto it, so this
--   file never narrows a metric another subsystem added, in any order.
-- * alert_rules.flow_scope (FlowFilter, canonical URL keys) and
--   alert_rules.flow_params (window, band, hold/clear evaluations, flap and
--   per-metric options) carry the NetFlow rule definition.
-- * netflow_alert_state keeps the per-(rule, subject) evaluation streaks and
--   transition history that hold-for-N, reset-after-N and flap suppression
--   need across evaluator restarts and between the two API workers.
-- * report_schedules_report_type_check gains the 'netflow_executive' preset
--   (NetFlow executive summary), widened in place like the metric check.
--
-- alert_rules / report_schedules are owned by the postgres superuser, so this
-- file is applied by a superuser. Idempotent: every statement is guarded.

BEGIN;

-- ── Alert metric vocabulary ────────────────────────────────────────────────
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('netflow_traffic_bps' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''netflow_traffic_bps'', ''netflow_endpoint_bytes'', '
             || '''netflow_exporter_silent'', ''netflow_interface_util_pct'', ''netflow_finding''))';
    END IF;
END $$;

-- ── Rule definition columns ────────────────────────────────────────────────
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS flow_scope JSONB NULL;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS flow_params JSONB NULL;

COMMENT ON COLUMN alert_rules.flow_scope IS
    'NetFlow rules: FlowFilter scope as canonical URL keys (exporter, iface, group, app, protocol, dst_port, talker, ...).';
COMMENT ON COLUMN alert_rules.flow_params IS
    'NetFlow rules: evaluation window, band, hold/clear evaluations, flap suppression and metric options.';

-- ── Evaluation state ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_alert_state (
    rule_id        UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    subject        VARCHAR(200) NOT NULL,
    state          VARCHAR(16) NOT NULL DEFAULT 'unknown'
                   CHECK (state IN ('ok', 'breach', 'unknown')),
    breach_streak  INT NOT NULL DEFAULT 0,
    clear_streak   INT NOT NULL DEFAULT 0,
    last_value     DOUBLE PRECISION NULL,
    last_reason    VARCHAR(64) NULL,
    last_eval_at   TIMESTAMPTZ NULL,
    transitions    JSONB NOT NULL DEFAULT '[]',
    flapping       BOOLEAN NOT NULL DEFAULT FALSE,
    context        JSONB NOT NULL DEFAULT '{}',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_netflow_alert_state_eval
    ON netflow_alert_state (last_eval_at DESC);

-- Open NetFlow alerts are looked up by (rule, condition key) every evaluation.
CREATE INDEX IF NOT EXISTS idx_alerts_netflow_dedupe
    ON alerts (rule_id, (metadata->>'netflow_dedupe'))
    WHERE metadata->>'netflow_dedupe' IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_alert_state TO zenplus;
    END IF;
END $$;

COMMENT ON TABLE netflow_alert_state IS
    'NetFlow alert evaluator: per-(rule, subject) streaks, last reading and flap history.';

-- ── Report schedule preset ─────────────────────────────────────────────────
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'report_schedules'::regclass AND conname = 'report_schedules_report_type_check';
    IF old_check IS NOT NULL AND position('netflow_executive' IN old_check) = 0 THEN
        ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_report_type_check;
        EXECUTE 'ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_report_type_check CHECK (('
             || old_check || ') OR report_type = ''netflow_executive'')';
    END IF;
END $$;

COMMIT;
