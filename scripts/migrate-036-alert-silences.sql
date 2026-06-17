-- Alert silences (snooze / mute) for server-scoped alerts.
--
-- A silence suppresses (re)raising of a specific alert condition on a specific
-- server. The condition is identified by the same dedupe key the evaluator and
-- server_health_service use (e.g. 'rule:<uuid>' or 'agent_offline').
--   until IS NULL   -> muted forever
--   until > now()   -> snoozed until that time (auto-expires)

CREATE TABLE IF NOT EXISTS alert_silences (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_id   UUID REFERENCES servers(id) ON DELETE CASCADE,
    dedupe      TEXT NOT NULL,
    until       TIMESTAMPTZ,
    reason      TEXT,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_silences_lookup ON alert_silences(server_id, dedupe);
