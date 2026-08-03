-- Device-scoped alert silences (snooze / mute).
--
-- alert_silences was introduced server-only (migration 036): the snooze
-- endpoint refused anything without a server_id, so device/network alerts
-- (utilization, errors, status changes, traps) could not be snoozed at all.
-- Add a device scope column; a silence row now carries exactly one of
-- server_id / device_id. The dedupe key for device alerts is derived from the
-- rule identity the evaluators already use:
--   rule:<rule_uuid>            (device-wide conditions: status, cpu, traps)
--   rule:<rule_uuid>:if:<idx>   (per-interface network metrics)

ALTER TABLE alert_silences
    ADD COLUMN IF NOT EXISTS device_id UUID REFERENCES devices(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_alert_silences_device ON alert_silences(device_id, dedupe);
