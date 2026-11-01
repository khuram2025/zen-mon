BEGIN;

CREATE TABLE IF NOT EXISTS server_event_collection (
    server_id UUID PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    gap_at TIMESTAMPTZ,
    status JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS server_lifecycle_events (
    server_id UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    event_uid VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    maintenance_suppressed BOOLEAN NOT NULL DEFAULT FALSE,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (server_id, event_uid)
);
CREATE INDEX IF NOT EXISTS ix_server_lifecycle_recent
    ON server_lifecycle_events (event_time DESC, server_id, kind);

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('host_restart' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''host_restart'', ''host_shutdown''))';
    END IF;
END $$;

INSERT INTO alert_rules (name,description,enabled,metric,operator,threshold,severity,
                         min_duration,cooldown,notify_channels,recovery_alert,priority)
SELECT x.name,x.description,TRUE,x.metric,'gt',0,x.severity,0,300,'[]'::jsonb,FALSE,x.priority
FROM (VALUES
    ('Server restart','New OS boot, restart request, or evidence of an unexpected shutdown. Historical backfill does not page.', 'host_restart','warning','P3'),
    ('Server shutdown','Windows reports an OS shutdown or a shutdown request. The notification can arrive after the next boot if the agent could not upload before power-off.', 'host_shutdown','warning','P3')
) AS x(name,description,metric,severity,priority)
WHERE NOT EXISTS (SELECT 1 FROM alert_rules r WHERE r.metric=x.metric AND r.server_id IS NULL);

UPDATE alert_rules SET description = 'Agent unresponsive while the server still answers ping: possible agent failure or system hang; the cause is unconfirmed. Device Down covers unreachable hosts.'
 WHERE name='Agent offline' AND metric='host_agent_offline'
   AND description LIKE 'The agent stopped reporting but the server still answers ping%';
COMMIT;
