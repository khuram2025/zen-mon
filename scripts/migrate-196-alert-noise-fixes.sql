-- migrate-196-alert-noise-fixes.sql
-- Alerting audit 2026-11-19, phase 1 (noise). Idempotent; changes no
-- threshold, channel or rule a person has edited.
--
-- 1. servers.health_issues: the issues of the server's last health
--    computation (level, reason, dedupe). Server health used to be mirrored
--    into "health_threshold" alert rows, opened by one agent upload above a
--    fixed limit and closed by the next one below it (no hold, no clear
--    level, no route). Health stays a colour on the server pages; this column
--    replaces the open alert rows as the carry-forward for partial uploads.
--
-- 2. Close the open health_threshold rows administratively. The server alert
--    rules (High / Critical CPU, memory, filesystem) cover the same limits
--    with a hold and a recovery. Closed rows stay in the history.
--
-- 3. The three oldest shipped rules (Device Down, High Latency, Packet Loss,
--    scripts/seed-devices.sql) were seeded with their hold in alert_rules.duration,
--    a column no evaluator reads; min_duration stayed 0. Where a rule's timers
--    and condition are still exactly as shipped (same name, metric, operator,
--    threshold, severity, cooldown and duration; min_duration 0; no
--    conditions or scope), the shipped hold is moved into min_duration:
--    Device Down 180 s (120 s on new installs) on top of the confirmed Down,
--    High Latency / Packet Loss 300 s on top of the confirmed Degraded. Saving
--    a rule in the editor writes min_duration and mirrors it into duration, so
--    a rule whose timers anyone set is never matched. (updated_at cannot tell:
--    migrate-168 touched every Device Down row.) Channels and escalation do
--    not matter here: adding a channel does not choose a hold.
--    A Device Down as shipped with no channel also gets its recovery notice
--    turned on; nothing is sent by that until someone adds a channel.
--
-- Latency and loss rules no longer fire on a move into Down (code change in
-- the same release): "no reply" is a reachability event, not 100% loss.

ALTER TABLE servers ADD COLUMN IF NOT EXISTS health_issues JSONB;

UPDATE alerts
   SET status = 'resolved',
       resolved_at = COALESCE(resolved_at, NOW()),
       metadata = COALESCE(metadata, '{}'::jsonb)
                  || jsonb_build_object('resolved_by', 'migration', 'retired', 'health_threshold')
 WHERE status IN ('pending', 'active', 'acknowledged')
   AND metadata->>'source' = 'health_threshold';

-- One statement, so the result does not depend on the order of updates.
UPDATE alert_rules
   SET min_duration = duration,
       recovery_alert = CASE WHEN name = 'Device Down'
                              AND COALESCE(jsonb_array_length(notify_channels), 0) = 0
                             THEN true ELSE recovery_alert END
 WHERE COALESCE(min_duration, 0) = 0
   AND conditions IS NULL
   AND device_id IS NULL AND group_id IS NULL AND device_type IS NULL
   AND location IS NULL AND scope_tag IS NULL
   AND (   (name = 'Device Down'  AND metric = 'ping_status' AND operator IN ('eq', '==') AND threshold = 0
            AND severity = 'critical' AND cooldown = 300 AND duration IN (120, 180))
        OR (name = 'High Latency' AND metric = 'rtt'         AND operator IN ('gt', '>')  AND threshold = 100
            AND severity = 'warning'  AND cooldown = 600 AND duration = 300)
        OR (name = 'Packet Loss'  AND metric = 'packet_loss' AND operator IN ('gt', '>')  AND threshold = 5
            AND severity = 'warning'  AND cooldown = 600 AND duration = 300));

-- 4. Recommended default rules for the whole appliance (app.services.recommended_rules):
--    an accepted one keeps its recommendation key ('default:<key>') with no
--    template. A template recommendation still needs its template.
-- Guarded: on a database before migrate-193 there is no pack_key yet (193 then
-- creates the constraint in its original form; this file runs again later in a
-- full upgrade only if it was not yet applied).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'alert_rules' AND column_name = 'pack_key') THEN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_pack_needs_template') THEN
            ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_pack_needs_template;
        END IF;
        ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_pack_needs_template
            CHECK (pack_key IS NULL OR profile_id IS NOT NULL OR pack_key LIKE 'default:%');
        EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS alert_rules_default_pack_once'
             || ' ON alert_rules (pack_key) WHERE profile_id IS NULL AND pack_key IS NOT NULL';
    END IF;
END $$;
