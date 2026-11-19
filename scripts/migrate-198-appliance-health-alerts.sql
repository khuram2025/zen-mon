-- migrate-198-appliance-health-alerts.sql
-- ZenPlus appliance health alerts (alerting plan G-1, docs/appliance-health-alerts.md).
--
-- The appliance did not alert on its own health: a full data disk, ClickHouse
-- not answering, a failed backup or update, an expiring licence, an
-- unsynchronised clock or a notification channel that keeps failing raised
-- nothing (a customer appliance had 180 failed deliveries in 30 days, unseen).
--
-- 1. alert_rules_metric_check gains the "appliance" rule family. Widened in
--    place (migrate-168 style): the current definition is kept verbatim and
--    the new metrics are OR-ed onto it.
-- 2. appliance_alert_state: the breach / clear holds of
--    app.services.appliance_alert_service, one row per rule + dimension
--    (channel id, sensor id, "clickhouse", or '' for appliance-wide metrics).
--    These readings have no stored history, so the evaluator keeps the hold
--    start times here, across passes and API workers.
-- 3. Enabled default rules, seeded once by metric + name. No channels and no
--    policy of their own: the priority default escalation policy routes them,
--    as for the other seeded rules. An operator may edit or disable them.
-- 4. Remote sensor offline is now a rule. The sensor sweep no longer writes
--    its rule-less "sensor_offline" alert row (it had no channel, no policy
--    and paged nobody); open rows of that kind are closed administratively
--    here (resolved_by = 'migration'), so the rule does not double them.
--
-- Idempotent; one transaction.

BEGIN;

-- ── 1. Metric constraint ─────────────────────────────────────────────────────
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('appliance_disk_pct' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''appliance_disk_pct'', ''appliance_db_down'', '
             || '''appliance_backup_failed'', ''appliance_update_failed'', ''appliance_license_days'', '
             || '''appliance_time_unsynced'', ''appliance_delivery_failures'', ''appliance_sensor_offline''))';
    END IF;
END $$;

-- ── 2. Hold state ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS appliance_alert_state (
    rule_id       UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    dimension     VARCHAR(128) NOT NULL DEFAULT '',
    breach_since  TIMESTAMPTZ,      -- first reading of the current unbroken breach
    clear_since   TIMESTAMPTZ,      -- first reading of the current unbroken clear run (open incident)
    last_seen_at  TIMESTAMPTZ,      -- last known (non-missing) reading
    last_value    DOUBLE PRECISION,
    alert_id      UUID,             -- the open incident this state belongs to
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, dimension)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON appliance_alert_state TO zenplus;
    END IF;
END $$;

-- ── 3. Default rules ─────────────────────────────────────────────────────────
INSERT INTO alert_rules (name, description, enabled, metric, operator, threshold, severity,
                         priority, min_duration, cooldown, notify_channels, recovery_alert)
SELECT x.name, x.description, TRUE, x.metric, x.operator, x.threshold, x.severity,
       x.priority, x.min_duration, 300, '[]'::jsonb, TRUE
FROM (VALUES
    ('Appliance disk high',
     'The ZenPlus data volume is more than 85 % used for 10 minutes.',
     'appliance_disk_pct', '>', 85, 'warning', 'P3', 600),
    ('Appliance disk critical',
     'The ZenPlus data volume is more than 95 % used for 10 minutes. The storage watchdog pauses ingestion when free space runs out.',
     'appliance_disk_pct', '>', 95, 'critical', 'P2', 600),
    ('Database down',
     'A ZenPlus database (ClickHouse) has not answered for 2 minutes. PostgreSQL itself cannot be watched from inside: use an external monitor on /api/v1/system/health.',
     'appliance_db_down', '>', 0, 'critical', 'P1', 120),
    ('Backup failed',
     'The last scheduled appliance backup failed, or none completed within its schedule plus a grace period.',
     'appliance_backup_failed', '>', 0, 'warning', 'P3', 0),
    ('Update failed',
     'The last ZenPlus update attempt failed or was rolled back.',
     'appliance_update_failed', '>', 0, 'warning', 'P3', 0),
    ('Licence expiring',
     'The ZenPlus licence expires in less than 30 days.',
     'appliance_license_days', '<', 30, 'warning', 'P3', 0),
    ('Licence expiring soon',
     'The ZenPlus licence expires in less than 7 days.',
     'appliance_license_days', '<', 7, 'critical', 'P2', 0),
    ('Clock not synchronised',
     'The appliance clock has not been synchronised by NTP for 15 minutes; timestamps and schedules may drift.',
     'appliance_time_unsynced', '>', 0, 'warning', 'P3', 900),
    ('Notification channel failing',
     'A notification channel failed 3 or more deliveries in the last hour. Give this rule more than one channel: a failing channel cannot announce itself.',
     'appliance_delivery_failures', '>=', 3, 'warning', 'P3', 0),
    ('Remote sensor offline',
     'A remote sensor stopped sending heartbeats for 3 minutes past its offline threshold; checks it runs are not being made.',
     'appliance_sensor_offline', '>', 0, 'critical', 'P2', 180)
) AS x(name, description, metric, operator, threshold, severity, priority, min_duration)
WHERE NOT EXISTS (
    SELECT 1 FROM alert_rules r WHERE r.metric = x.metric AND r.name = x.name
);

-- ── 4. Retire the rule-less sensor_offline rows ──────────────────────────────
UPDATE alerts
   SET status = 'resolved', resolved_at = NOW(),
       metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
           'resolved_by', 'migration',
           'resolved_reason', 'Remote sensor offline is now an alert rule (migrate-198)')
 WHERE rule_id IS NULL
   AND metadata->>'dedupe' = 'sensor_offline'
   AND status IN ('pending', 'active', 'acknowledged');

COMMIT;
