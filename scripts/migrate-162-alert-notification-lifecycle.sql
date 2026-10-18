-- Migration 162: metric alert lifecycle and durable notification delivery.
--
-- Field report (October 2026): a server whose memory hovered between 90.1% and
-- 91.5% against a 90% rule produced 121 separate "rose above" emails in 48 h,
-- and two interface-error emails arrived twice, seconds apart, because both
-- API workers evaluated and sent the same incident.
--
--  * alert_rules.clear_duration   seconds the healthy (cleared) condition must
--                                 hold before a metric incident resolves.
--                                 NULL = the evaluator default (stable
--                                 recovery, see docs/alert-notification-policy.md);
--                                 0 = resolve on the first healthy sample.
--  * alert_delivery_keys          one row per (incident, lifecycle event,
--                                 channel, recipient). A worker claims the key
--                                 before it sends, so concurrent workers,
--                                 retries and restarts never re-send a known
--                                 transition; the outcome (accepted, failed,
--                                 uncertain) stays auditable.
--
-- Additive and idempotent; one transaction so the grants cannot be left behind.

BEGIN;

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS clear_duration INTEGER;

CREATE TABLE IF NOT EXISTS alert_delivery_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id     UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    event        VARCHAR(64) NOT NULL,
    channel_id   UUID NOT NULL,
    recipient    TEXT NOT NULL DEFAULT '',
    state        VARCHAR(16) NOT NULL DEFAULT 'claimed',
    error        TEXT,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    CONSTRAINT alert_delivery_keys_state_check
        CHECK (state IN ('claimed', 'accepted', 'failed', 'uncertain'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_delivery_keys_event
    ON alert_delivery_keys (alert_id, event, channel_id, recipient);
CREATE INDEX IF NOT EXISTS idx_alert_delivery_keys_claimed
    ON alert_delivery_keys (claimed_at);

COMMENT ON COLUMN alert_rules.clear_duration IS
    'Seconds a metric condition must stay cleared before the incident resolves; NULL = evaluator default, 0 = immediate.';
COMMENT ON TABLE alert_delivery_keys IS
    'Durable delivery claims: (alert, lifecycle event, channel, recipient) is sent at most once.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON alert_delivery_keys TO zenplus;
    END IF;
END $$;

COMMIT;
