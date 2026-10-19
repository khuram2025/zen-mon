-- Migration 166: priority for open built-in (rule-less) alerts.
--
-- Built-in alerts are raised by the platform itself, not by an alert rule
-- (app.services.builtin_alert_sources): agent offline, remote sensor offline
-- or degraded, configuration backup failed / configuration changed (NCM),
-- software baseline drift and the fixed server health thresholds. From
-- 1.24.21 they are stamped with a priority when raised and can be routed by
-- that priority's default escalation policy. Rows raised by earlier releases
-- carry no stamp, so their priority would be derived from severity (critical
-- -> P2): a critical CPU health threshold would rank as a P2 outage. This
-- stamps the still-open ones with the registry's priority:
--
--   agent_offline P2, sensor_offline P2, sensor_degraded P3,
--   ncm_backup_failed P3, ncm_config_changed P4, baseline_drift P4,
--   health_threshold critical P3 / otherwise P4.
--
-- UPDATE only, open rows only, and only rows without a stamp (a stamped
-- incident is never re-ranked), so re-running it changes nothing. Resolved
-- history keeps whatever it had. No schema change: alerts.priority and its
-- CHECK constraint come from migration 164.

BEGIN;

UPDATE alerts a
   SET priority = CASE
         WHEN a.metadata->>'source' = 'agent_offline' AND a.server_id IS NOT NULL THEN 'P2'
         WHEN a.metadata->>'source' = 'sensor_offline' AND a.sensor_id IS NOT NULL THEN 'P2'
         WHEN a.metadata->>'source' = 'sensor_degraded' AND a.sensor_id IS NOT NULL THEN 'P3'
         WHEN a.metadata->>'ncm' = 'true' AND a.metadata->>'ncm_event' = 'backup.failed'
              AND a.device_id IS NOT NULL THEN 'P3'
         WHEN a.metadata->>'ncm' = 'true' AND a.metadata->>'ncm_event' = 'config.changed'
              AND a.device_id IS NOT NULL THEN 'P4'
         WHEN a.metadata->>'source' = 'baseline' AND a.server_id IS NOT NULL THEN 'P4'
         WHEN a.metadata->>'source' = 'health_threshold' AND a.server_id IS NOT NULL
              AND a.severity = 'critical' THEN 'P3'
         WHEN a.metadata->>'source' = 'health_threshold' AND a.server_id IS NOT NULL THEN 'P4'
       END
 WHERE a.status IN ('pending', 'active', 'acknowledged')
   AND a.rule_id IS NULL
   AND a.priority IS NULL
   AND (   (a.metadata->>'source' IN ('agent_offline', 'baseline', 'health_threshold')
            AND a.server_id IS NOT NULL)
        OR (a.metadata->>'source' IN ('sensor_offline', 'sensor_degraded') AND a.sensor_id IS NOT NULL)
        OR (a.metadata->>'ncm' = 'true' AND a.metadata->>'ncm_event' IN ('backup.failed', 'config.changed')
            AND a.device_id IS NOT NULL));

COMMIT;
