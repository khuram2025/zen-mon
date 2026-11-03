-- Independent incident authorities delegate delivery, never incident ownership.
BEGIN;
CREATE TABLE notification_site_identity (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
    id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()
);
INSERT INTO notification_site_identity(singleton) VALUES(true);
CREATE TABLE notification_site_peers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    site_id UUID NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    enabled BOOLEAN NOT NULL DEFAULT true,
    config JSONB NOT NULL DEFAULT '{}',
    token_hash TEXT,
    channel_map JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(site_id,direction)
);
CREATE TABLE notification_site_policies (
    channel_id UUID PRIMARY KEY REFERENCES notification_channels(id) ON DELETE CASCADE,
    senders JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE notification_site_inbox (
    id UUID PRIMARY KEY,
    origin_site UUID NOT NULL,
    alert_id UUID NOT NULL,
    event VARCHAR(64) NOT NULL,
    channel_id UUID NOT NULL,
    recipient TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing','accepted','failed','uncertain')),
    claim_token UUID NOT NULL,
    grant_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    UNIQUE(origin_site,alert_id,event,channel_id,recipient)
);
CREATE TABLE notification_site_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id UUID REFERENCES alerts(id) ON DELETE SET NULL,
    event VARCHAR(64) NOT NULL,
    channel_id UUID NOT NULL,
    recipient TEXT NOT NULL,
    sender_id UUID,
    outcome TEXT NOT NULL CHECK(outcome IN ('unavailable','accepted','failed','uncertain')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX notification_site_attempts_alert ON notification_site_attempts(alert_id,created_at);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT ON notification_site_identity TO zenplus;
        GRANT SELECT,INSERT,UPDATE,DELETE ON notification_site_peers,notification_site_policies,
            notification_site_inbox,notification_site_attempts TO zenplus;
    END IF;
END $$;
COMMIT;
