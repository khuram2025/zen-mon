-- Apply before starting senders. Stop legacy metric evaluators during cutover.
BEGIN;
CREATE TABLE IF NOT EXISTS notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence BIGSERIAL UNIQUE,
    alert_id UUID REFERENCES alerts(id) ON DELETE SET NULL,
    event VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    routes JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','accepted','uncertain','held','blocked','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token UUID,
    lease_expires_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    last_error TEXT,
    UNIQUE (alert_id, event)
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(due_at, sequence)
    WHERE status IN ('pending','processing','blocked');
CREATE INDEX IF NOT EXISTS idx_notification_outbox_incident
    ON notification_outbox(alert_id, sequence);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON notification_outbox TO zenplus;
        GRANT USAGE, SELECT ON SEQUENCE notification_outbox_sequence_seq TO zenplus;
    END IF;
END $$;
COMMIT;
