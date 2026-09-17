-- Development network events: idempotent syslog ingestion and rule support.
BEGIN;
CREATE TABLE IF NOT EXISTS network_events (
    id UUID PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip INET NOT NULL,
    device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    facility SMALLINT NOT NULL CHECK (facility BETWEEN 0 AND 23),
    severity SMALLINT NOT NULL CHECK (severity BETWEEN 0 AND 7),
    reported_hostname TEXT,
    application TEXT,
    message TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_network_events_time ON network_events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_network_events_device_time ON network_events(device_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_trap_event ON alerts(rule_id, (metadata->>'trap_event_id'))
    WHERE metadata->>'trap_event_id' IS NOT NULL;
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
    WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('syslog' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (' || old_check || ' OR metric = ''syslog'')';
    END IF;
END $$;
COMMIT;
