-- Alert storm (correlation) policy.
--
-- When more than N assets raise the same kind of event (down, reboot, ...) in
-- one scope within a window, the per-asset notifications are replaced by one
-- combined notification per channel and recipient, followed by periodic
-- updates and a closing summary. Settings live in
-- system_settings['alert_storm'] and are OFF by default: this migration changes
-- no notification behaviour by itself. Logic: app.services.alert_storm.
--
-- Combined notifications reuse notification_digests (reason 'storm', group key
-- 'storm:<storm id>' or 'storm:<storm id>:final').

-- Every storm-eligible trigger, once per incident: the sliding window counts
-- distinct assets per storm key (event family + scope).
CREATE TABLE IF NOT EXISTS alert_storm_events (
    alert_id     UUID PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
    storm_key    TEXT NOT NULL,
    family       VARCHAR(40) NOT NULL,
    asset_key    TEXT NOT NULL,
    asset_label  TEXT,
    at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_storm_events_key_at ON alert_storm_events (storm_key, at DESC);

CREATE TABLE IF NOT EXISTS alert_storms (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    storm_key      TEXT NOT NULL,
    family         VARCHAR(40) NOT NULL,
    scope_label    TEXT,
    status         VARCHAR(16) NOT NULL DEFAULT 'active',
    threshold      INTEGER NOT NULL,
    window_minutes INTEGER NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    declared_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_event_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at       TIMESTAMPTZ,
    CONSTRAINT alert_storms_status_check CHECK (status IN ('active', 'ended'))
);
-- At most one running storm per key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_storms_active ON alert_storms (storm_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_alert_storms_started ON alert_storms (started_at DESC);

-- Incidents that belong to a storm. ``held`` is true when the incident's own
-- trigger was replaced by the combined notification (its recovery is then
-- reported in the storm updates too); false for incidents notified on their
-- own before the storm was declared.
CREATE TABLE IF NOT EXISTS alert_storm_members (
    storm_id     UUID NOT NULL REFERENCES alert_storms(id) ON DELETE CASCADE,
    alert_id     UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    asset_key    TEXT NOT NULL,
    asset_label  TEXT,
    held         BOOLEAN NOT NULL DEFAULT false,
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recovered_at TIMESTAMPTZ,
    PRIMARY KEY (storm_id, alert_id)
);
CREATE INDEX IF NOT EXISTS idx_alert_storm_members_alert ON alert_storm_members (alert_id);
