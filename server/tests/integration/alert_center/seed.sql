-- Alert Center integration fixture. Loaded by test_alert_center_e2e.py into a
-- DISPOSABLE database (never production). __PASSWORD_HASH__ is replaced with a
-- bcrypt hash of 'Test12345' for the webop/viewer1/ro1/netop users.
-- scoped + unscoped test users
INSERT INTO users (username, email, password_hash, role, full_name, is_active, scope_tags) VALUES
 ('webop', 'webop@x.test', '__PASSWORD_HASH__', 'operator', 'Web Operator', true, '["web-team"]'),
 ('viewer1', 'viewer1@x.test', '__PASSWORD_HASH__', 'viewer', 'Viewer One', true, '[]'),
 ('ro1', 'ro1@x.test', '__PASSWORD_HASH__', 'read_only', 'Read Only', true, '[]'),
 ('netop', 'netop@x.test', '__PASSWORD_HASH__', 'operator', 'Net Operator', true, '[]')
ON CONFLICT DO NOTHING;
UPDATE devices SET tags = '["web-team"]'::jsonb WHERE id IN (SELECT id FROM devices ORDER BY hostname LIMIT 3);
INSERT INTO servers (hostname, display_name, tags, primary_ip) VALUES
 ('srv-web-01', 'WEB-01', '["web-team"]', '10.9.0.11'), ('srv-db-01', 'DB-01', '[]', '10.9.0.12');
INSERT INTO service_checks (name, check_type, target_host, tags) VALUES
 ('Portal HTTPS', 'http', 'portal.x.test', ARRAY['web-team']), ('Mail SMTP', 'tcp', 'mail.x.test', ARRAY['mail']);
INSERT INTO sensors (name, tags) VALUES ('branch-sensor-1', '[]');
INSERT INTO notification_channels (name, type, config, enabled) VALUES
 ('NOC webhook', 'webhook', '{"url":"https://127.0.0.1:9/hook"}', true),
 ('Disabled email', 'email', '{"recipients":"a@x.test"}', false);
INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, trigger_on, recovery_alert, min_duration, cooldown, conditions)
 SELECT 'T Device Down', 'ping_status', '==', 0, 'critical', jsonb_build_array((SELECT id::text FROM notification_channels WHERE name='NOC webhook')), 'down', true, 0, 300, NULL;
INSERT INTO alert_rules (name, metric, operator, threshold, severity, group_id, notify_channels, conditions)
 SELECT 'Core CPU', 'cpu', '>', 90, 'warning', (SELECT id FROM device_groups ORDER BY name LIMIT 1), '[]', '[{"metric":"cpu","operator":">","threshold":90,"reset_threshold":80}]';
INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, min_duration)
 SELECT 'Host CPU high', 'host_cpu_pct', '>', 85, 'warning', jsonb_build_array((SELECT id::text FROM notification_channels WHERE name='NOC webhook')), 95;
INSERT INTO alert_rules (name, metric, operator, threshold, severity, device_id, notify_channels, scope_tag)
 SELECT 'Web RTT', 'rtt', '>', 200, 'warning', (SELECT id FROM devices WHERE tags @> '["web-team"]' ORDER BY hostname LIMIT 1), '[]', NULL;
-- 1,300 active device-down alerts spread over 45 days (oldest first)
INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
SELECT d.id, (SELECT id FROM alert_rules WHERE name='T Device Down'), 'active', CASE WHEN g % 10 = 0 THEN 'warning' ELSE 'critical' END,
       'Device Down: ' || d.hostname || ' is DOWN #' || g, NOW() - (g || ' minutes')::interval * 50,
       jsonb_build_object('old_status','up','new_status','down','is_recovery',false,'if_index', g::text)
FROM generate_series(1,1300) g JOIN LATERAL (SELECT id, hostname FROM devices ORDER BY hostname OFFSET (g % 27) LIMIT 1) d ON true;
-- server alerts (host metric + agent offline)
INSERT INTO alerts (server_id, severity, message, status, metadata, triggered_at)
SELECT s.id, 'warning', 'Host CPU high: ' || s.hostname || ' — 97.0%', 'active',
       jsonb_build_object('source','host_metric','dedupe','rule:' || (SELECT id FROM alert_rules WHERE name='Host CPU high'),
                          'rule_id', (SELECT id FROM alert_rules WHERE name='Host CPU high')::text, 'metric','host_cpu_pct','value',97,'threshold',85),
       NOW() - interval '3 hours'
FROM servers s WHERE s.hostname LIKE 'srv-%';
INSERT INTO alerts (server_id, severity, message, status, metadata, triggered_at)
SELECT s.id, 'critical', 'Agent offline: ' || s.hostname, 'active', '{"source":"agent_offline","dedupe":"agent_offline"}', NOW() - interval '9 days'
FROM servers s WHERE s.hostname = 'srv-db-01';
-- service + sensor + apm
INSERT INTO alerts (service_check_id, status, severity, message, triggered_at, metadata)
SELECT id, 'active', 'critical', 'Portal HTTPS is DOWN', NOW() - interval '40 minutes', '{"new_status":"down","old_status":"up","error":"TLS handshake timeout"}' FROM service_checks WHERE name='Portal HTTPS';
INSERT INTO alerts (sensor_id, status, severity, message, metadata) SELECT id, 'active', 'warning', 'Sensor branch-sensor-1 stale', '{"dedupe":"stale","source":"sensor_health"}' FROM sensors;
INSERT INTO alerts (status, severity, message, metadata, triggered_at) VALUES ('active','critical','Synthetic scenario DOWN: Login journey', '{"source":"apm_synthetic","monitor_name":"Login journey","monitor_id":"m1","dedupe":"synthetic:m1"}', NOW() - interval '2 hours');
-- history: recovered, manual, notices over 30 days
INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
SELECT d.id, (SELECT id FROM alert_rules WHERE name='T Device Down'), 'resolved', 'critical', 'Device Down: ' || d.hostname || ' is DOWN (hist ' || g || ')',
       NOW() - (g || ' minutes')::interval * 7, NOW() - (g || ' minutes')::interval * 7 + ((g % 90) || ' minutes')::interval,
       CASE WHEN g % 5 = 0 THEN jsonb_build_object('resolved_by','manual','new_status','down')
            ELSE jsonb_build_object('resolved_by_recovery', true,'new_status','down') END
FROM generate_series(1,6000) g JOIN LATERAL (SELECT id, hostname FROM devices ORDER BY hostname OFFSET (g % 27) LIMIT 1) d ON true;
INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
SELECT d.id, (SELECT id FROM alert_rules WHERE name='T Device Down'), 'resolved', 'critical', 'Device Down: ' || d.hostname || ' recovered — UP',
       NOW() - (g || ' minutes')::interval * 11, NOW() - (g || ' minutes')::interval * 11, '{"is_recovery":true,"new_status":"up"}'
FROM generate_series(1,700) g JOIN LATERAL (SELECT id, hostname FROM devices ORDER BY hostname OFFSET (g % 27) LIMIT 1) d ON true;
-- an old incident closed today
INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
SELECT id, (SELECT id FROM alert_rules WHERE name='T Device Down'), 'resolved', 'critical', 'OLD incident closed today', NOW() - interval '35 days', NOW() - interval '10 minutes', '{"resolved_by_recovery":true,"new_status":"down"}'
FROM devices ORDER BY hostname LIMIT 1;
