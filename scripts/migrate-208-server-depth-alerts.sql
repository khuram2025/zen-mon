-- migrate-208-server-depth-alerts.sql
-- Server depth metrics for host alert rules (alerting plan G-9 and G-10,
-- docs/alert-notification-policy.md "Server rules"; app.services.host_depth_metrics).
--
-- The agent already collects these; host_alert_service now evaluates them:
--
--   host_swap_pct              swap used / total, % (averaged like memory)
--   host_disk_latency_ms       slower of average read/write time per I/O, ms, per disk
--                              (Linux agents; the Windows agent's disk times are unusable)
--   host_nic_errors            receive + transmit errors per second, per interface
--   host_clock_skew_s          |agent clock - appliance clock| from every upload, s
--   host_fs_days_left          days until a filesystem is full at its 7-day growth, per mount
--   host_auto_service_stopped  an automatic service stopped unexpectedly, per service
--
-- alert_rules_metric_check is widened in place (migrate-198/199 style): the
-- current definition is kept verbatim and the new metrics are OR-ed onto it,
-- so this file never narrows a metric another subsystem added, in any order.
-- The snapshot metrics keep their holds in service_metric_alert_state
-- (migrate-199); no new table.
--
-- alert_rules is owned by the postgres superuser, so this file is applied by a
-- superuser. Idempotent; one transaction.

BEGIN;

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('host_auto_service_stopped' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''host_swap_pct'', ''host_disk_latency_ms'', '
             || '''host_nic_errors'', ''host_clock_skew_s'', ''host_fs_days_left'', '
             || '''host_auto_service_stopped''))';
    END IF;
END $$;

-- Clock skew rules read every upload's measurement in a time window.
CREATE INDEX IF NOT EXISTS idx_agent_host_result_batches_completed
    ON agent_host_result_batches (completed_at, server_id)
    WHERE completed_at IS NOT NULL;

COMMIT;
