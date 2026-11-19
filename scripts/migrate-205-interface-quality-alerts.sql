-- Migration 205: interface quality alerts (alerting audit 2026-11-19, gap G-12).
--
-- Interface error/discard rules counted errors per poll, not as a share of
-- traffic, and nothing watched an interface that keeps going up and down or
-- renegotiates its speed (docs/alert-notification-policy.md "Interface quality").
--
-- * alert_rules_metric_check gains the keys network_alert_service evaluates:
--     if_error_rate_pct    errors as a % of packets in a poll interval (worse direction)
--     if_discard_rate_pct  discards as a % of packets in a poll interval (worse direction)
--     if_flaps             oper-status changes in the rule's window
--     if_speed_changed     1 while a link speed change is recent
--   Widened in place (migrate-199 style): the current definition is kept
--   verbatim and the new list OR-ed onto it.
-- * device_interfaces records negotiated speed changes: up_speed_bps is the
--   speed last seen while the interface was oper-up; when an up interface
--   reports a different non-zero speed, previous_speed_bps keeps the old one
--   and speed_changed_at is stamped. Speeds reported while the link is down
--   (many devices report a default speed then) are never compared, so a link
--   that drops and returns at its usual speed is not a change. A BEFORE
--   trigger keeps this for every writer (controller poller, sensors), so the
--   poller needs no change. Existing rows start with no recorded speed: the
--   first poll after the upgrade sets the baseline and raises nothing.
--
-- alert_rules is owned by the postgres superuser, so this file is applied by a
-- superuser. Idempotent: every statement is guarded.

BEGIN;

DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('if_error_rate_pct' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''if_error_rate_pct'', ''if_discard_rate_pct'', '
             || '''if_flaps'', ''if_speed_changed''))';
    END IF;
END $$;

ALTER TABLE device_interfaces ADD COLUMN IF NOT EXISTS up_speed_bps BIGINT NULL;
ALTER TABLE device_interfaces ADD COLUMN IF NOT EXISTS previous_speed_bps BIGINT NULL;
ALTER TABLE device_interfaces ADD COLUMN IF NOT EXISTS speed_changed_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN device_interfaces.up_speed_bps IS
    'Speed (bps) last reported while the interface was oper-up (migrate-205 trigger).';
COMMENT ON COLUMN device_interfaces.previous_speed_bps IS
    'The up speed before the latest speed change (migrate-205 trigger).';
COMMENT ON COLUMN device_interfaces.speed_changed_at IS
    'When an up interface last reported a different non-zero speed (migrate-205 trigger).';

CREATE OR REPLACE FUNCTION device_interfaces_track_speed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF lower(COALESCE(NEW.oper_status, '')) IN ('up', '1') AND COALESCE(NEW.if_speed, 0) > 0 THEN
        IF TG_OP = 'UPDATE' AND COALESCE(OLD.up_speed_bps, 0) > 0
           AND OLD.up_speed_bps <> NEW.if_speed THEN
            NEW.previous_speed_bps := OLD.up_speed_bps;
            NEW.speed_changed_at := NOW();
        END IF;
        NEW.up_speed_bps := NEW.if_speed;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_device_interfaces_track_speed ON device_interfaces;
CREATE TRIGGER trg_device_interfaces_track_speed
    BEFORE INSERT OR UPDATE OF if_speed, oper_status ON device_interfaces
    FOR EACH ROW EXECUTE FUNCTION device_interfaces_track_speed();

CREATE INDEX IF NOT EXISTS idx_device_interfaces_speed_changed
    ON device_interfaces (speed_changed_at) WHERE speed_changed_at IS NOT NULL;

COMMIT;
