-- Authenticated and workflow service checks use the shared checker from sensor 1.23.5.
CREATE OR REPLACE FUNCTION sensor_supports_service_auth(version TEXT) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE AS $$
 SELECT COALESCE((SELECT ARRAY[m[1]::INTEGER,m[2]::INTEGER,m[3]::INTEGER] >= ARRAY[1,23,5]
 FROM regexp_matches(version, '^(?:sensor-)?v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})([-+].*)?$') AS m), FALSE);
$$;
