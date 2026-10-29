-- Sensor release strings include prerelease and build metadata. Keep the
-- database width aligned with the 128-character enrollment/heartbeat contract.
-- Widen existing columns only; replay never narrows a later schema.
DO $$
DECLARE
    field_name text;
    width integer;
    vantage_definition text;
BEGIN
    SELECT character_maximum_length INTO width
      FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = 'sensors' AND column_name = 'version';
    IF width IS NOT NULL AND width < 128 THEN
        -- The capability predicate depends on sensors.version. Temporarily
        -- remove that dependency within this atomic DO statement, preserving
        -- the view's OID, owner, grants and dependent views. Readers cannot see
        -- this empty definition before the transaction restores the original.
        -- Unexpected additional dependencies fail and roll back; never CASCADE.
        vantage_definition := pg_get_viewdef('service_monitoring_vantages'::regclass, true);
        CREATE OR REPLACE VIEW service_monitoring_vantages AS
            SELECT NULL::uuid AS service_check_id, NULL::text AS poller_id,
                   NULL::uuid AS sensor_id WHERE false;
    END IF;
    FOREACH field_name IN ARRAY ARRAY['version', 'min_supported_version'] LOOP
        SELECT character_maximum_length INTO width
          FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = 'sensors'
           AND column_name = field_name;
        IF width IS NOT NULL AND width < 128 THEN
            EXECUTE format('ALTER TABLE sensors ALTER COLUMN %I TYPE VARCHAR(128)', field_name);
        END IF;
    END LOOP;
    IF vantage_definition IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW service_monitoring_vantages AS ' || vantage_definition;
    END IF;
END
$$;
