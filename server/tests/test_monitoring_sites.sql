-- Run after migration 110 in a transaction and ROLLBACK. No test state is retained.
DO $$
DECLARE d UUID := gen_random_uuid(); c UUID := gen_random_uuid();
        a UUID := gen_random_uuid(); b UUID := gen_random_uuid(); n INTEGER;
BEGIN
  INSERT INTO devices(id,hostname,ip_address) VALUES(d,'multisite-test-'||d,'192.0.2.110');
  INSERT INTO service_checks(id,name,check_type,target_host) VALUES(c,'multisite-test-'||c,'icmp','192.0.2.110');
  INSERT INTO sensors(id,name,status) VALUES(a,'multisite-test-'||a,'online'),(b,'multisite-test-'||b,'online');
  IF (SELECT owner_kind FROM device_polling_owner WHERE device_id=d) <> 'central' THEN RAISE EXCEPTION 'new device must default to controller'; END IF;
  INSERT INTO monitoring_policies(target_type,target_id,controller_enabled) VALUES('device',d,true),('service_check',c,true);
  INSERT INTO sensor_assignments(sensor_id,target_type,target_id) VALUES(a,'device',d),(b,'device',d),(a,'service_check',c),(b,'service_check',c);
  SELECT count(*) INTO n FROM device_monitoring_vantages WHERE device_id=d;
  IF n<>3 THEN RAISE EXCEPTION 'device should have controller plus two sensors: %',n; END IF;
  SELECT count(*) INTO n FROM service_monitoring_vantages WHERE service_check_id=c;
  IF n<>3 THEN RAISE EXCEPTION 'service should have controller plus two sensors: %',n; END IF;
  IF (SELECT owner_kind FROM device_polling_owner WHERE device_id=d) <> 'central' THEN RAISE EXCEPTION 'secondary probes must not replace controller status'; END IF;
  UPDATE monitoring_policies SET controller_enabled=false WHERE target_id IN(d,c);
  IF EXISTS(SELECT 1 FROM device_monitoring_vantages WHERE device_id=d AND poller_id='central') THEN RAISE EXCEPTION 'controller was deselected'; END IF;
  IF EXISTS(SELECT 1 FROM service_monitoring_vantages WHERE service_check_id=c AND poller_id='central') THEN RAISE EXCEPTION 'service controller was deselected'; END IF;
  UPDATE sensors SET status='offline' WHERE id=a;
  IF NOT EXISTS(SELECT 1 FROM device_monitoring_vantages WHERE device_id=d AND sensor_id=a) THEN RAISE EXCEPTION 'offline selection must be retained, not silently failed over'; END IF;
  DELETE FROM sensor_assignments WHERE target_id=d AND sensor_id=b;
  IF EXISTS(SELECT 1 FROM device_monitoring_vantages WHERE device_id=d AND sensor_id=b) THEN RAISE EXCEPTION 'removed sensor must lose target access'; END IF;
  UPDATE monitoring_policies SET controller_enabled=true WHERE target_id=d;
  DELETE FROM sensor_assignments WHERE target_id=d;
  SELECT count(*) INTO n FROM device_monitoring_vantages WHERE device_id=d;
  IF n<>1 THEN RAISE EXCEPTION 'controller-only selection must exclude former probes'; END IF;
  UPDATE service_checks SET check_type='http', target_url='https://example.test', workflow_steps='[{"name":"Health","url":"https://example.test","method":"GET"}]'::jsonb WHERE id=c;
  UPDATE sensors SET version='1.23.4' WHERE id IN(a,b);
  IF EXISTS(SELECT 1 FROM service_monitoring_vantages WHERE service_check_id=c AND sensor_id IS NOT NULL) THEN RAISE EXCEPTION 'old probes must not receive workflows'; END IF;
  UPDATE sensors SET version='1.23.5' WHERE id=a;
  IF NOT EXISTS(SELECT 1 FROM service_monitoring_vantages WHERE service_check_id=c AND sensor_id=a) THEN RAISE EXCEPTION 'updated probe must receive assigned workflows'; END IF;
  IF EXISTS(SELECT 1 FROM service_monitoring_vantages WHERE service_check_id=c AND sensor_id=b) THEN RAISE EXCEPTION 'unupdated probe must stay excluded'; END IF;
  DELETE FROM sensor_assignments WHERE target_id=c AND sensor_id=a;
  IF EXISTS(SELECT 1 FROM service_monitoring_vantages WHERE service_check_id=c AND sensor_id=a) THEN RAISE EXCEPTION 'revoked workflow assignment must lose access'; END IF;
END $$;
