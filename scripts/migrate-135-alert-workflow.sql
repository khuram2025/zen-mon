-- Migration 135: Alert Center workflow — ownership, lifecycle events,
-- delivery receipts and trigger-time policy snapshots (Alerts review
-- A06/A07/A10/A23/A24, F01/F04/F08).
--
-- alerts.assigned_to / assigned_at
--   Who owns an incident. Acknowledgement records who accepted it; assignment
--   is the explicit hand-over and can change while the incident is open.
--
-- alert_events
--   Immutable lifecycle history written by the API in the same transaction as
--   the state change it describes (acknowledge, resolve, assign, comment,
--   snooze, unsnooze, suppression expiry, ...). Evaluator transitions are
--   derived from the alert row itself, so producers need no changes.
--
-- alert_deliveries
--   One row per notification attempt per channel: accepted (the transport or
--   provider took it), failed (with a sanitised error) or skipped (channel
--   disabled, no recipients, no gateway). Configured routing on the rule is
--   policy; these rows are what actually happened. rule_id carries no FK so
--   the history survives the rule being deleted.
--
-- alert_silences.alert_id / condition_label
--   The alert a silence was created from and a human description of the
--   condition, for the suppression workspace.
--
-- Policy snapshot trigger
--   Copies the owning rule (conditions, scope target, routing with channel
--   names, escalation) into alerts.metadata.policy_snapshot at INSERT time,
--   for every producer, so an incident keeps the policy it fired under even
--   after the rule is edited or deleted. A failure never blocks the insert.
--
-- Backfill
--   Resolutions recorded in the audit log as manual (alert.resolve) or as a
--   snooze get the matching metadata.resolved_by marker, so closed-incident
--   metrics can separate human closure from genuine recovery for history too.

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assigned_to UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_alerts_assigned_open
    ON alerts(assigned_to) WHERE status IN ('pending', 'active', 'acknowledged');
CREATE INDEX IF NOT EXISTS idx_alerts_status_triggered
    ON alerts(status, triggered_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_alerts_resolved_at
    ON alerts(resolved_at DESC) WHERE resolved_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_rule_any ON alerts(rule_id);

CREATE TABLE IF NOT EXISTS alert_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id    UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind        VARCHAR(40) NOT NULL,
    actor_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_name  VARCHAR(255),
    source      VARCHAR(20) NOT NULL DEFAULT 'user',
    reason      TEXT,
    old_status  VARCHAR(20),
    new_status  VARCHAR(20),
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id, at);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id      UUID REFERENCES alerts(id) ON DELETE CASCADE,
    rule_id       UUID,
    channel_id    UUID,
    channel_name  VARCHAR(255),
    channel_type  VARCHAR(40),
    kind          VARCHAR(30) NOT NULL DEFAULT 'trigger',
    outcome       VARCHAR(20) NOT NULL,
    error         TEXT,
    latency_ms    INTEGER,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_alert ON alert_deliveries(alert_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_time ON alert_deliveries(attempted_at DESC);

ALTER TABLE alert_silences ADD COLUMN IF NOT EXISTS alert_id UUID REFERENCES alerts(id) ON DELETE SET NULL;
ALTER TABLE alert_silences ADD COLUMN IF NOT EXISTS condition_label TEXT;

CREATE OR REPLACE FUNCTION zp_alert_policy_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    rid  uuid;
    snap jsonb;
BEGIN
    IF COALESCE(NEW.metadata, '{}'::jsonb) ? 'policy_snapshot'
       OR COALESCE(NEW.metadata->>'is_recovery', 'false') = 'true' THEN
        RETURN NEW;
    END IF;
    rid := NEW.rule_id;
    IF rid IS NULL AND (NEW.metadata->>'rule_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        rid := (NEW.metadata->>'rule_id')::uuid;
    END IF;
    IF rid IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT jsonb_build_object(
               'rule_id', r.id,
               'name', r.name,
               'enabled', r.enabled,
               'severity', r.severity,
               'metric', r.metric,
               'operator', r.operator,
               'threshold', r.threshold,
               'conditions', r.conditions,
               'condition_logic', r.condition_logic,
               'trigger_on', r.trigger_on,
               'recovery_alert', r.recovery_alert,
               'min_duration', r.min_duration,
               'cooldown', r.cooldown,
               'target', r.target,
               'escalation_levels', r.escalation_levels,
               'notify_channels', COALESCE((
                   SELECT jsonb_agg(jsonb_build_object('id', c.id, 'name', c.name,
                                                       'type', c.type, 'enabled', c.enabled)
                                    ORDER BY c.name)
                   FROM notification_channels c
                   WHERE jsonb_typeof(r.notify_channels) = 'array'
                     AND c.id::text IN (SELECT jsonb_array_elements_text(r.notify_channels))
               ), '[]'::jsonb),
               'rule_updated_at', r.updated_at,
               'captured_at', NOW())
      INTO snap
      FROM alert_rules r
     WHERE r.id = rid;
    IF snap IS NOT NULL THEN
        NEW.metadata := COALESCE(NEW.metadata, '{}'::jsonb) || jsonb_build_object('policy_snapshot', snap);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_alert_policy_snapshot ON alerts;
CREATE TRIGGER trg_alert_policy_snapshot
    BEFORE INSERT ON alerts
    FOR EACH ROW EXECUTE FUNCTION zp_alert_policy_snapshot();

UPDATE alerts a
   SET metadata = COALESCE(a.metadata, '{}'::jsonb)
                  || jsonb_build_object('resolved_by', 'manual',
                                        'resolved_by_user', l.actor_id::text)
  FROM (SELECT DISTINCT ON (resource_id) resource_id, actor_id
          FROM audit_logs
         WHERE action = 'alert.resolve' AND resource_type = 'alert'
         ORDER BY resource_id, created_at DESC) l
 WHERE a.id::text = l.resource_id
   AND a.status = 'resolved'
   AND NOT (COALESCE(a.metadata, '{}'::jsonb) ? 'resolved_by')
   AND NOT (COALESCE(a.metadata, '{}'::jsonb) ? 'resolved_by_recovery');

UPDATE alerts a
   SET metadata = COALESCE(a.metadata, '{}'::jsonb)
                  || jsonb_build_object('resolved_by', 'snooze',
                                        'snoozed_by', l.actor_id::text)
  FROM (SELECT DISTINCT ON (resource_id) resource_id, actor_id
          FROM audit_logs
         WHERE action = 'alert.snooze' AND resource_type = 'alert'
         ORDER BY resource_id, created_at DESC) l
 WHERE a.id::text = l.resource_id
   AND a.status = 'resolved'
   AND NOT (COALESCE(a.metadata, '{}'::jsonb) ? 'resolved_by');
