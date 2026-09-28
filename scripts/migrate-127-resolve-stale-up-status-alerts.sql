-- Migration 127: resolve stale "is UP" status alerts.
--
-- A rule with trigger_on='any' and the seeded condition ping_status == 0
-- ("Device Down") matched a device's first good ping (unknown -> up): the
-- engine did not evaluate ping_status and treated only down/degraded -> up as
-- a recovery. The result was a Critical, permanently active row reading
-- "Device Down: <host> (<ip>) is UP" on every newly added device — the
-- recovery and supersede resolvers only closed rows whose metadata.new_status
-- was down/degraded, so nothing ever closed it. Service checks with an 'any'
-- rule collected the same rows.
--
-- The engine no longer raises these, and any transition to 'up' now closes
-- every open status alert on the object. This closes the rows already raised.
--
-- Data-only and idempotent: a second run matches nothing.

BEGIN;

UPDATE alerts
   SET status = 'resolved',
       resolved_at = COALESCE(resolved_at, NOW()),
       metadata = COALESCE(metadata, '{}'::jsonb)
                  || jsonb_build_object(
                         'auto_resolved', 'migrate-127 stale up-status alert',
                         'resolved_by', 'migration')
 WHERE status IN ('pending', 'active', 'acknowledged')
   AND lower(COALESCE(metadata->>'new_status', '')) = 'up'
   AND COALESCE(metadata->>'is_recovery', 'false') <> 'true';

COMMIT;
