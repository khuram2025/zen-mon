-- migrate-213-discovery-rule-schedules.sql
-- Discovery rules: a schedule of the rule's own (docs/discovery-rule-engine.md
-- "Schedules").
--
--   discovery_engine_rules.schedule            NULL: the rule follows the shared
--                                              engine schedule (as before);
--                                              else {"kind": "once" | "hourly" |
--                                              "weekly" | "monthly", ...} in the
--                                              rule's own IANA time zone.
--   discovery_engine_rules.schedule_last_slot  the most recent slot of the rule's
--                                              own schedule that was run (or
--                                              marked as the starting point when
--                                              the schedule was saved).
--
-- Server site, "Add to group" and custom field actions need no schema: they
-- write servers.site_id, device_group_members (source 'static') and
-- custom_field_values, and record their values in discovery_engine_assignments
-- like every other action. Idempotent; no data is changed.

ALTER TABLE discovery_engine_rules ADD COLUMN IF NOT EXISTS schedule JSONB;
ALTER TABLE discovery_engine_rules ADD COLUMN IF NOT EXISTS schedule_last_slot TIMESTAMPTZ;

ALTER TABLE discovery_engine_rules DROP CONSTRAINT IF EXISTS discovery_engine_rules_schedule;
ALTER TABLE discovery_engine_rules ADD CONSTRAINT discovery_engine_rules_schedule CHECK (
    schedule IS NULL OR (jsonb_typeof(schedule) = 'object'
                         AND schedule->>'kind' IN ('once', 'hourly', 'weekly', 'monthly')));

COMMENT ON COLUMN discovery_engine_rules.schedule IS
  'The rule''s own schedule for the "scheduled" trigger; NULL follows the shared engine schedule.';
COMMENT ON COLUMN discovery_engine_rules.schedule_last_slot IS
  'Most recent slot of the rule''s own schedule that ran (or the starting point when it was saved).';
