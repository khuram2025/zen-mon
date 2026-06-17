-- Server-scoped host-metric alert rules.
--
-- Extends the existing alert_rules/alerts tables so rules can target a server
-- (or all servers when server_id IS NULL) and fire on host metrics collected by
-- the agent (CPU, memory, filesystem, disk I/O, a watched service/process).
-- Alerts already carry server_id (see server_health_service); this just makes
-- sure the column exists and is indexed.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS server_id UUID REFERENCES servers(id) ON DELETE CASCADE;
-- Optional target name for service/process rules (service name, process name, or mount).
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS target VARCHAR(255);

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS server_id UUID REFERENCES servers(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_alert_rules_server ON alert_rules(server_id) WHERE server_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_server_status ON alerts(server_id, status) WHERE server_id IS NOT NULL;

-- Allow host-metric values in addition to the existing device/service/trap set.
ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (
  metric IN (
    'ping_status','rtt','packet_loss','jitter','service_status',
    'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
    'if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status',
    'session_count','vpn_tunnel_state','ha_state',
    'trap',
    -- host (server agent) metrics
    'host_cpu_pct','host_memory_pct','host_filesystem_pct','host_disk_util_pct',
    'host_service_down','host_process_down'
  )
);

-- Sensible default rules that apply to every agent server (server_id NULL).
-- Thresholds mirror the health computation in server_health_service.py.
-- Seeded once; re-running is a no-op (matched by name + host scope).
INSERT INTO alert_rules (name, description, enabled, metric, operator, threshold, severity, min_duration, notify_channels)
SELECT v.name, v.description, TRUE, v.metric, 'gt', v.threshold, v.severity, v.min_duration, '[]'::jsonb
FROM (VALUES
  ('High CPU usage',        'CPU above 90% (all servers)',        'host_cpu_pct',        90.0, 'warning',  300),
  ('Critical CPU usage',    'CPU above 98% (all servers)',        'host_cpu_pct',        98.0, 'critical', 300),
  ('High memory usage',     'Memory above 90% (all servers)',     'host_memory_pct',     90.0, 'warning',  300),
  ('Critical memory usage', 'Memory above 97% (all servers)',     'host_memory_pct',     97.0, 'critical', 300),
  ('Filesystem filling up', 'A filesystem above 85% (all servers)','host_filesystem_pct', 85.0, 'warning',  0),
  ('Filesystem nearly full','A filesystem above 95% (all servers)','host_filesystem_pct', 95.0, 'critical', 0)
) AS v(name, description, metric, threshold, severity, min_duration)
WHERE NOT EXISTS (
  SELECT 1 FROM alert_rules r
  WHERE r.name = v.name AND r.metric = v.metric AND r.server_id IS NULL
);
