-- migrate-206-security-ops-alerts.sql
-- Security and operations alerts (alerting plan G-16, docs/appliance-health-alerts.md).
--
-- ZenPlus raised nothing when someone kept failing to sign in, when an
-- administrator changed accounts, roles, sign-in settings or alert rules, or
-- when a scheduled report run failed or was only partly delivered. These are
-- three more metrics of the "appliance" rule family
-- (app.services.appliance_alert_service), with its lifecycle and hold table
-- (appliance_alert_state, migrate-198):
--
--   appliance_login_failures  failed sign-ins in the last 15 minutes, per
--                             existing account, else per source address
--   appliance_admin_changes   security-relevant administrative changes in the
--                             last 15 minutes (event-like: one incident per burst)
--   appliance_report_failed   a schedule's latest finished run failed or was
--                             partial (per schedule)
--
-- 1. alert_rules_metric_check gains the three metrics. Widened in place
--    (migrate-198 style): the current definition is kept verbatim and the new
--    metrics are OR-ed onto it.
-- 2. Default rules, seeded once by metric + name, with no channels of their
--    own (the priority default escalation policy routes them). "Repeated
--    failed sign-ins" and "Scheduled report failed" are enabled.
--    "Administrative changes" is seeded turned off: watching administrators is
--    a policy decision for the operator.
--
-- Idempotent; one transaction.

BEGIN;

-- ── 1. Metric constraint ─────────────────────────────────────────────────────
DO $$
DECLARE old_check TEXT;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO old_check FROM pg_constraint
     WHERE conrelid = 'alert_rules'::regclass AND conname = 'alert_rules_metric_check';
    IF old_check IS NOT NULL AND position('appliance_login_failures' IN old_check) = 0 THEN
        ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
        EXECUTE 'ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (('
             || old_check || ') OR metric IN (''appliance_login_failures'', '
             || '''appliance_admin_changes'', ''appliance_report_failed''))';
    END IF;
END $$;

-- ── 2. Default rules ─────────────────────────────────────────────────────────
INSERT INTO alert_rules (name, description, enabled, metric, operator, threshold, severity,
                         priority, min_duration, clear_duration, cooldown, notify_channels,
                         recovery_alert)
SELECT x.name, x.description, x.enabled, x.metric, x.operator, x.threshold, x.severity,
       x.priority, 0, x.clear_duration, 300, '[]'::jsonb, TRUE
FROM (VALUES
    ('Repeated failed sign-ins',
     'Ten or more failed sign-ins in 15 minutes for one account, or from one address for names that are no account. Clears after 15 minutes below the limit.',
     TRUE, 'appliance_login_failures', '>=', 10, 'warning', 'P3', 900),
    ('Administrative changes',
     'Security-relevant administrative changes: users, roles, sign-in settings, access lists, certificates, new keys or tokens, and alert rules, channels or policies deleted or turned off. One alert per burst; clears after 15 minutes without a change. Turned off by default.',
     FALSE, 'appliance_admin_changes', '>=', 1, 'info', 'P4', 900),
    ('Scheduled report failed',
     'The latest run of a report schedule failed or was only partly delivered (after its delivery retries). Clears on the next good run.',
     TRUE, 'appliance_report_failed', '>', 0, 'warning', 'P3', 0)
) AS x(name, description, enabled, metric, operator, threshold, severity, priority, clear_duration)
WHERE NOT EXISTS (
    SELECT 1 FROM alert_rules r WHERE r.metric = x.metric AND r.name = x.name
);

COMMIT;
