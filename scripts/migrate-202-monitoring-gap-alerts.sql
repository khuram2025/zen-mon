-- Migration 202: monitoring gap alerts (alerting audit 2026-11-19, gap G-2).
--
-- A device that answers ping while every SNMP poll fails, and an agent that
-- heartbeats but sends no metrics, used to look healthy: nothing recorded
-- the failed polls and no rule could watch them (docs/monitoring-gap-alerts.md).
--
-- * device_snmp_poll_state: one row per device the controller poller polls
--   over SNMP, written after every poll (poller/internal/store). A poll
--   counts as answered when the device returned anything at all; a poll the
--   per-device budget cut short after a reply is still answered. failing_since
--   is the start of the current run of unanswered polls (NULL while polls are
--   answered). Devices polled only by a remote sensor have no row.
-- * alert_rules_metric_check gains snmp_unreachable and agent_no_metrics
--   (widened in place, migrate-199 style: the current definition is kept
--   verbatim and the new list OR-ed onto it).
-- * Open gap incidents are looked up by (rule, metadata gap_key).
--
-- The breach / clear hold state reuses service_metric_alert_state (migrate-199).
-- alert_rules is owned by the postgres superuser, so this file is applied by a
-- superuser. Idempotent: every statement is guarded.

BEGIN;

CREATE TABLE IF NOT EXISTS device_snmp_poll_state (
    device_id             UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    last_attempt_at       TIMESTAMPTZ NOT NULL,
    last_ok_at            TIMESTAMPTZ NULL,
    failing_since         TIMESTAMPTZ NULL,
    last_failure_at       TIMESTAMPTZ NULL,
    last_error            TEXT NULL,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_device_snmp_poll_state_failing
    ON device_snmp_poll_state (failing_since) WHERE failing_since IS NOT NULL;

COMMENT ON TABLE device_snmp_poll_state IS
    'Outcome of the latest SNMP polls per device (controller poller): answered or not, and since when polls have failed.';

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('snmp_unreachable' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''snmp_unreachable'', ''agent_no_metrics''))';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_alerts_gap_key
    ON alerts (rule_id, (metadata->>'gap_key'))
    WHERE metadata->>'gap_key' IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_snmp_poll_state TO zenplus;
    END IF;
END $$;

COMMIT;
