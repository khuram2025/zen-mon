-- Manual interface speed/bandwidth override for utilization calculations.
-- SNMP poller continues to update if_speed; configured_speed_bps is operator-set.

ALTER TABLE device_interfaces
    ADD COLUMN IF NOT EXISTS configured_speed_bps BIGINT NULL;

COMMENT ON COLUMN device_interfaces.configured_speed_bps IS
    'Operator override (bps) for utilization; NULL = use SNMP if_speed';
