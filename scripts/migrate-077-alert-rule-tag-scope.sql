-- Migration 077: scope alert rules by device tag
--
-- Alert rule scope was one of device / group / device_type / location, all of
-- them fixed attributes of a device. Tags are the one grouping operators
-- maintain by hand, so "alert on every device tagged core-switch" had to be
-- rebuilt by hand every time the tag membership changed.
--
-- `scope_tag` holds a single tag name, matching the existing scope columns,
-- which are single-valued and mutually exclusive. It deliberately mirrors
-- device_maintenance.scope_tag / service_check_groups.scope_tag: same 120-char
-- width, same "assignments live in devices.tags JSONB" matching via
-- jsonb_exists(), so the GIN index on devices.tags serves this too.
--
-- No FK to the tags registry: assignments are JSONB, the registry holds
-- metadata only, and a rename propagates through tag_service. A dangling
-- scope_tag simply matches no devices, which is the safe failure mode.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE alert_rules
  ADD COLUMN IF NOT EXISTS scope_tag VARCHAR(120);

COMMENT ON COLUMN alert_rules.scope_tag IS
  'Device tag this rule is scoped to; matched against devices.tags JSONB. NULL = not tag-scoped.';

COMMIT;
