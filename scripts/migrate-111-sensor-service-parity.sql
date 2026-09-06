-- Authenticated and workflow service checks use the shared checker from sensor 1.23.5.
CREATE OR REPLACE FUNCTION sensor_supports_service_auth(version TEXT) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE AS $$
 SELECT COALESCE((SELECT ARRAY[m[1]::INTEGER,m[2]::INTEGER,m[3]::INTEGER] >= ARRAY[1,23,5]
 FROM regexp_matches(version, '^v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})([-+].*)?$') AS m), FALSE);
$$;

CREATE OR REPLACE VIEW service_monitoring_vantages AS
WITH remote AS (
    SELECT sc.id AS service_check_id, a.sensor_id
    FROM service_checks sc JOIN sensor_assignments a ON a.target_type = 'service_check' AND a.target_id = sc.id
    JOIN sensors s ON s.id = a.sensor_id
    WHERE (sc.credential_id IS NULL AND jsonb_array_length(COALESCE(sc.workflow_steps, '[]'::jsonb)) = 0 OR sensor_supports_service_auth(s.version))
      AND (s.status IN ('online', 'degraded', 'offline') OR EXISTS (
        SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'service_check' AND p.target_id = sc.id))
    UNION
    SELECT sc.id, sc.default_sensor_id FROM service_checks sc JOIN sensors s ON s.id = sc.default_sensor_id
    WHERE (sc.credential_id IS NULL AND jsonb_array_length(COALESCE(sc.workflow_steps, '[]'::jsonb)) = 0 OR sensor_supports_service_auth(s.version))
      AND s.status IN ('online', 'degraded', 'offline')
      AND NOT EXISTS (SELECT 1 FROM monitoring_policies p WHERE p.target_type = 'service_check' AND p.target_id = sc.id)
)
SELECT service_check_id, sensor_id::TEXT AS poller_id, sensor_id FROM remote
UNION ALL
SELECT sc.id, 'central'::TEXT, NULL::UUID FROM service_checks sc
LEFT JOIN monitoring_policies p ON p.target_type = 'service_check' AND p.target_id = sc.id
WHERE COALESCE(p.controller_enabled, NOT EXISTS (SELECT 1 FROM remote r WHERE r.service_check_id = sc.id));
