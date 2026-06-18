-- Network-device (SNMP) alert rules.
--
-- Device/service alerting was event-driven (the Go poller pushes ping/trap
-- status changes to alert_engine) and host-metric alerting is a periodic
-- ClickHouse evaluator (host_alert_service). Polled SNMP metrics — interface
-- utilization/errors/oper-status and device cpu/memory/temperature — arrive
-- continuously in ClickHouse like host metrics, so they get the same periodic
-- evaluator treatment (network_alert_service.py), keyed off the metric value.
--
-- The metric CHECK constraint already whitelisted the interface/device keys
-- (migrate-035). This adds the two derived keys the evaluator introduces
-- (if_util_pct, bgp_neighbor_down) and seeds fleet-wide default rules.
--
-- Per-interface scoping reuses the existing `target` column (added in
-- migrate-035): an empty target means "all monitored interfaces on the
-- in-scope device(s)"; a non-empty target matches an interface by if_name /
-- if_descr / if_alias (case-insensitive substring) or exact if_index.

-- Allow the derived network metric keys in addition to the existing set.
-- (Re-declares the full list — keep in lockstep with migrate-035.)
ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (
  metric IN (
    'ping_status','rtt','packet_loss','jitter','service_status',
    'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
    'if_in_bps','if_out_bps','if_util_pct','if_errors','if_discards','if_oper_status',
    'session_count','vpn_tunnel_state','ha_state','bgp_neighbor_down',
    'trap',
    -- host (server agent) metrics
    'host_cpu_pct','host_memory_pct','host_filesystem_pct','host_disk_util_pct',
    'host_service_down','host_process_down'
  )
);

-- Index the alerts table for the evaluator's per-rule/per-device dedupe lookup.
CREATE INDEX IF NOT EXISTS idx_alerts_rule_device_status
  ON alerts(rule_id, device_id, status) WHERE device_id IS NOT NULL;

-- Sensible default rules that apply to every SNMP-monitored device
-- (device_id NULL, group_id NULL, server_id NULL). Seeded once; re-running is a
-- no-op (matched by name + metric + un-scoped). Operators use the long form the
-- engine accepts (gt/eq) to match the host-rule seed style.
INSERT INTO alert_rules
  (name, description, enabled, metric, operator, threshold, duration,
   severity, min_duration, trigger_on, notify_channels)
SELECT v.name, v.description, TRUE, v.metric, v.operator, v.threshold, v.min_duration,
       v.severity, v.min_duration, 'any', '[]'::jsonb
FROM (VALUES
  ('Interface down',
   'An admin-up, monitored interface reports oper-status down (all devices)',
   'if_oper_status', 'eq',  2.0,   'critical', 0),
  ('Interface high utilization',
   'Interface in/out utilization above 90% of link speed (all devices)',
   'if_util_pct',    'gt',  90.0,  'warning',  300),
  ('Interface errors high',
   'More than 100 in/out errors on an interface within the window (all devices)',
   'if_errors',      'gt',  100.0, 'warning',  300),
  ('High device CPU (SNMP)',
   'SNMP-reported CPU above 90% (all devices)',
   'cpu',            'gt',  90.0,  'warning',  300),
  ('High device memory (SNMP)',
   'SNMP-reported memory above 90% (all devices)',
   'memory',         'gt',  90.0,  'warning',  300),
  ('Device reboot detected',
   'Device sysUpTime reset since last poll — likely an unexpected reboot (all devices)',
   'uptime_reset',   'eq',  1.0,   'warning',  0)
) AS v(name, description, metric, operator, threshold, severity, min_duration)
WHERE NOT EXISTS (
  SELECT 1 FROM alert_rules r
  WHERE r.name = v.name AND r.metric = v.metric
    AND r.device_id IS NULL AND r.group_id IS NULL AND r.server_id IS NULL
);
