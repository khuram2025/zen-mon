-- Explicit multi-site selections; old assignments retain their existing behavior.
CREATE TABLE IF NOT EXISTS monitoring_policies (
    target_type TEXT NOT NULL CHECK (target_type IN ('device', 'service_check')),
    target_id UUID NOT NULL,
    controller_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (target_type, target_id)
);

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
WHERE COALESCE(p.controller_enabled, NOT EXISTS (SELECT 1 FROM remote r WHERE r.device_id = d.id));

-- Keep a stable primary source for the existing device health/alert surface.
-- Other selected sensors still collect and retain their own measurements.
CREATE OR REPLACE VIEW device_polling_owner AS
SELECT d.id AS device_id,
       CASE WHEN v.poller_id = 'central' THEN 'central' ELSE 'sensor' END::TEXT AS owner_kind,
       v.sensor_id, 'monitoring-sites'::TEXT AS source, 100::INTEGER AS priority
FROM devices d LEFT JOIN LATERAL (
    SELECT poller_id, sensor_id FROM device_monitoring_vantages v WHERE v.device_id = d.id
    ORDER BY (poller_id = 'central') DESC, poller_id LIMIT 1
) v ON TRUE;

CREATE OR REPLACE VIEW service_monitoring_vantages AS
WITH remote AS (
    SELECT sc.id AS service_check_id, a.sensor_id
    FROM service_checks sc JOIN sensor_assignments a ON a.target_type = 'service_check' AND a.target_id = sc.id
    JOIN sensors s ON s.id = a.sensor_id
    WHERE sc.credential_id IS NULL AND jsonb_array_length(COALESCE(sc.workflow_steps, '[]'::jsonb)) = 0
      AND (s.status IN ('online', 'degraded', 'offline') OR EXISTS (
        SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'service_check' AND p.target_id = sc.id))
    UNION
    SELECT sc.id, sc.default_sensor_id FROM service_checks sc JOIN sensors s ON s.id = sc.default_sensor_id
    WHERE sc.credential_id IS NULL AND jsonb_array_length(COALESCE(sc.workflow_steps, '[]'::jsonb)) = 0
      AND s.status IN ('online', 'degraded', 'offline')
      AND NOT EXISTS (SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'service_check' AND p.target_id = sc.id)
)
SELECT service_check_id, sensor_id::TEXT AS poller_id, sensor_id FROM remote
UNION ALL
SELECT sc.id, 'central'::TEXT, NULL::UUID FROM service_checks sc
LEFT JOIN monitoring_policies p ON p.target_type = 'service_check' AND p.target_id = sc.id
WHERE COALESCE(p.controller_enabled, NOT EXISTS (SELECT 1 FROM remote r WHERE r.service_check_id = sc.id));

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON monitoring_policies TO zenplus;
    GRANT SELECT ON device_monitoring_vantages, service_monitoring_vantages TO zenplus;
  END IF;
END $$;
