-- Explicit sensor-to-controller fallback for devices with one remote collector.
-- Existing multi-vantage selections and disabled-by-default policies are preserved.
ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS controller_fallback_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS controller_fallback_active BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS fallback_changed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS fallback_healthy_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS fallback_recovery_seconds INTEGER NOT NULL DEFAULT 120
        CHECK (fallback_recovery_seconds BETWEEN 30 AND 3600);

CREATE OR REPLACE VIEW device_monitoring_vantages AS
WITH candidates AS (
    SELECT a.target_id AS device_id, a.sensor_id FROM sensor_assignments a WHERE a.target_type = 'device'
    UNION
    SELECT d.id, a.sensor_id FROM devices d JOIN sensor_assignments a
      ON a.target_type = 'group' AND a.target_id = d.group_id
      WHERE NOT EXISTS (SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'device' AND p.target_id = d.id)
    UNION
    SELECT d.id, d.default_sensor_id FROM devices d WHERE d.default_sensor_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'device' AND p.target_id = d.id)
), remote AS (
    SELECT c.device_id, c.sensor_id FROM candidates c JOIN sensors s ON s.id = c.sensor_id
    WHERE s.status IN ('online', 'degraded', 'offline') OR EXISTS (
      SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'device' AND p.target_id = c.device_id)
)
SELECT device_id, sensor_id::TEXT AS poller_id, sensor_id FROM remote
UNION ALL
SELECT d.id, 'central'::TEXT, NULL::UUID FROM devices d
LEFT JOIN monitoring_policies p ON p.target_type = 'device' AND p.target_id = d.id
WHERE COALESCE(p.controller_enabled, NOT EXISTS (SELECT 1 FROM remote r WHERE r.device_id = d.id))
   OR EXISTS (
       SELECT 1 FROM remote r JOIN sensors s ON s.id = r.sensor_id
        WHERE r.device_id = d.id AND s.controller_fallback_active
          AND NOT EXISTS (SELECT 1 FROM remote other
                           WHERE other.device_id = d.id AND other.sensor_id <> r.sensor_id)
   );
