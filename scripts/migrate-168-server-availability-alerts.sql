-- Migration 168: server availability alerting.
--
-- A server watched only by its agent could go down without paging anyone:
-- "Device Down" read ping_status from the poller, which never sees an agent
-- server, and the staleness sweep's built-in "agent offline" alert had no rule,
-- so no channel and no escalation policy (server_availability.py).
--
--  * alert_rules_metric_check gains host_agent_offline: the agent stopped
--    reporting while the server still answers ping (monitoring lost, not
--    down). Widened in place (migrate-143 style): the current definition is
--    kept verbatim and the new metric is OR-ed onto it.
--  * Seeds one "Agent offline" rule for every agent server: warning, P3,
--    recovery notice on, no direct channels and no policy of its own, so it
--    follows the P3 default escalation policy. Seeded once by name + metric +
--    host scope, like migrate-035; an operator may edit or disable it.
--  * The seeded Device Down description (seed-devices.sql) now says it covers
--    servers too. Only the untouched seed text is replaced.
--
-- Device Down rules themselves need no change: a ping_status rule triggered on
-- down that is not tied to a device, group, type or location now also pages
-- for agent servers. Idempotent; one transaction.

BEGIN;

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('host_agent_offline' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''host_agent_offline''))';
    END IF;
END $$;

INSERT INTO alert_rules (name, description, enabled, metric, operator, threshold, severity,
                         min_duration, notify_channels, recovery_alert, priority)
SELECT 'Agent offline',
       'The agent stopped reporting but the server still answers ping (monitoring lost). '
       'A server that cannot be reached at all is paged by Device Down instead.',
       TRUE, 'host_agent_offline', 'gt', 0, 'warning', 0, '[]'::jsonb, TRUE, 'P3'
WHERE NOT EXISTS (
    SELECT 1 FROM alert_rules r
    WHERE r.metric = 'host_agent_offline' AND r.server_id IS NULL
);

UPDATE alert_rules
   SET description = 'Alert when any device or server goes down', updated_at = NOW()
 WHERE name = 'Device Down' AND metric = 'ping_status'
   AND description = 'Alert when any device goes down';

COMMIT;
