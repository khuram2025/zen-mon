-- Migration 164: escalation matrix, on-call schedules and notification grouping.
--
-- Follows the October 2026 field review (docs/alert-notification-policy.md):
--
--  * Priorities P1 Critical / P2 High / P3 Warning / P4 Information sit beside
--    severity. alert_rules.priority NULL = derived from severity
--    (critical -> P2, warning -> P3, info -> P4; P1 is chosen explicitly);
--    alerts.priority is stamped when the incident is raised.
--  * escalation_policies + escalation_steps: reusable staged routing with an
--    acknowledgement target, repeat and unresolved-review intervals, an
--    acknowledgement expiry and a fallback route. A policy can be the default
--    for priorities; a rule may name one (escalation_policy_id). Rules with
--    neither keep their inline escalation_levels exactly as before.
--  * oncall_schedules / oncall_layers / oncall_overrides: rotations in an IANA
--    time zone, with time-boxed overrides; resolved to users at send time.
--  * user_contact_methods + users.timezone: how a responder is reached.
--  * notification_digests / notification_digest_items: warnings held for a
--    short group wait and sent as one summary per group and channel. Every
--    held message records its reason and release condition; critical and
--    P1/P2 notifications never wait.
--
-- Additive and idempotent; one transaction so the grants cannot be left behind.

BEGIN;

-- Priorities -----------------------------------------------------------------
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS priority VARCHAR(2);
ALTER TABLE alerts      ADD COLUMN IF NOT EXISTS priority VARCHAR(2);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_priority_check') THEN
        ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_priority_check
            CHECK (priority IS NULL OR priority IN ('P1', 'P2', 'P3', 'P4'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alerts_priority_check') THEN
        ALTER TABLE alerts ADD CONSTRAINT alerts_priority_check
            CHECK (priority IS NULL OR priority IN ('P1', 'P2', 'P3', 'P4'));
    END IF;
END $$;

-- People -----------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR(64);

CREATE TABLE IF NOT EXISTS user_contact_methods (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        VARCHAR(16) NOT NULL,
    address     VARCHAR(255) NOT NULL,
    label       VARCHAR(100),
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT user_contact_methods_kind_check CHECK (kind IN ('email', 'sms'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_contact_methods
    ON user_contact_methods (user_id, kind, lower(address));

-- On-call ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oncall_schedules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(255) NOT NULL,
    description  TEXT,
    timezone     VARCHAR(64) NOT NULL DEFAULT 'UTC',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oncall_schedules_name ON oncall_schedules (lower(name));

-- A layer rotates its ordered members every shift_hours from handoff_at.
-- restrict_to optionally limits when the layer is on duty:
-- [{"days": [0..6], "start": "HH:MM", "end": "HH:MM"}] in the schedule zone.
-- Higher position wins when two layers are on duty at the same time.
CREATE TABLE IF NOT EXISTS oncall_layers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id  UUID NOT NULL REFERENCES oncall_schedules(id) ON DELETE CASCADE,
    name         VARCHAR(100) NOT NULL DEFAULT 'Layer',
    position     INTEGER NOT NULL DEFAULT 0,
    shift_hours  INTEGER NOT NULL DEFAULT 168,
    handoff_at   TIMESTAMPTZ NOT NULL,
    members      UUID[] NOT NULL DEFAULT '{}',
    restrict_to  JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT oncall_layers_shift_check CHECK (shift_hours BETWEEN 1 AND 8760)
);
CREATE INDEX IF NOT EXISTS idx_oncall_layers_schedule ON oncall_layers (schedule_id, position);

CREATE TABLE IF NOT EXISTS oncall_overrides (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id  UUID NOT NULL REFERENCES oncall_schedules(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    starts_at    TIMESTAMPTZ NOT NULL,
    ends_at      TIMESTAMPTZ NOT NULL,
    reason       TEXT,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT oncall_overrides_window_check CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_oncall_overrides_window ON oncall_overrides (schedule_id, starts_at, ends_at);

-- Escalation policies ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS escalation_policies (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                     VARCHAR(255) NOT NULL,
    description              TEXT,
    enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    -- Priorities this policy routes when a rule names no policy.
    default_priorities       VARCHAR(2)[] NOT NULL DEFAULT '{}',
    ack_target_minutes       INTEGER,
    -- Re-notify the current step while unacknowledged (NULL = no repeat).
    repeat_minutes           INTEGER,
    max_repeats              INTEGER NOT NULL DEFAULT 3,
    -- Unresolved-incident review updates; continue after acknowledgement.
    review_minutes           INTEGER,
    -- An acknowledgement older than this re-opens escalation (NULL = never).
    ack_expiry_minutes       INTEGER,
    -- Where a step goes when it resolves to nobody or every delivery failed.
    fallback_targets         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by               UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_escalation_policies_name ON escalation_policies (lower(name));

-- targets: [{"type": "schedule"|"user"|"channel", "id": "<uuid>"}]
CREATE TABLE IF NOT EXISTS escalation_steps (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id      UUID NOT NULL REFERENCES escalation_policies(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    delay_minutes  INTEGER NOT NULL DEFAULT 0,
    name           VARCHAR(100),
    targets        JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT escalation_steps_delay_check CHECK (delay_minutes BETWEEN 0 AND 10080)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_escalation_steps_position ON escalation_steps (policy_id, position);

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS escalation_policy_id UUID
    REFERENCES escalation_policies(id) ON DELETE SET NULL;

-- Grouping / digests -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_digests (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_key          TEXT NOT NULL,
    group_label        TEXT,
    channel_id         UUID,
    recipient          TEXT NOT NULL DEFAULT '',
    status             VARCHAR(16) NOT NULL DEFAULT 'open',
    reason             TEXT NOT NULL,
    release_condition  TEXT NOT NULL,
    opened_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    release_at         TIMESTAMPTZ NOT NULL,
    sent_at            TIMESTAMPTZ,
    item_count         INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    CONSTRAINT notification_digests_status_check
        CHECK (status IN ('open', 'sending', 'sent', 'failed', 'cancelled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_digests_open
    ON notification_digests (group_key, COALESCE(channel_id, '00000000-0000-0000-0000-000000000000'::uuid), recipient)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_notification_digests_due
    ON notification_digests (release_at) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS notification_digest_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    digest_id   UUID NOT NULL REFERENCES notification_digests(id) ON DELETE CASCADE,
    alert_id    UUID REFERENCES alerts(id) ON DELETE SET NULL,
    event       VARCHAR(64) NOT NULL,
    severity    VARCHAR(20),
    summary     JSONB NOT NULL DEFAULT '{}'::jsonb,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notification_digest_items_digest ON notification_digest_items (digest_id);
CREATE INDEX IF NOT EXISTS idx_notification_digest_items_alert ON notification_digest_items (alert_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON user_contact_methods, oncall_schedules, oncall_layers,
            oncall_overrides, escalation_policies, escalation_steps, notification_digests,
            notification_digest_items TO zenplus;
    END IF;
END $$;

COMMIT;
