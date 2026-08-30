-- Migration 100: multi-level alert escalation
--
-- Alert rules could notify one set of channels at trigger time and nothing
-- afterwards: an unacknowledged 2am page that nobody picked up stayed with the
-- level-1 on-call forever. Professional SLA practice is tiered escalation —
-- if the alert is not acknowledged or resolved within N minutes, page the next
-- tier.
--
-- `escalation_levels` is a JSONB array, ordered by level:
--   [{"after_minutes": 15, "notify_channels": ["<uuid>", ...],
--     "repeat_every_minutes": 30 | null}, ...]
--
-- Semantics (enforced by alert_escalation_service):
--   * Level N fires when the alert has been active AND unacknowledged for
--     `after_minutes` since it was triggered.
--   * Acknowledging the alert stops all further escalation; resolving it sends
--     an all-clear to every channel that was escalated to.
--   * `repeat_every_minutes` on a level re-sends that level's notification on
--     that cadence until ack/resolve; alert_rules.max_repeat caps the number
--     of repeats (0 = unlimited).
--
-- Per-alert escalation state lives in alerts.metadata->'escalation' (level,
-- last_at, repeats, notified channel set) — no schema change needed there.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE alert_rules
  ADD COLUMN IF NOT EXISTS escalation_levels JSONB;

COMMENT ON COLUMN alert_rules.escalation_levels IS
  'Ordered escalation tiers: [{after_minutes, notify_channels[], repeat_every_minutes}]. NULL/empty = no escalation.';

COMMIT;
