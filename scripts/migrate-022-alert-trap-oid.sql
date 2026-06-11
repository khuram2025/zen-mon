-- migrate-022-alert-trap-oid.sql
-- E2: trap-based alert rules (alerts-only path lives in the engine).
-- Adds an optional trap-OID filter column and allows metric='trap'.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS trap_oid TEXT DEFAULT NULL;

ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (
  metric IN (
    'ping_status','rtt','packet_loss','jitter','service_status',
    'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
    'if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status',
    'session_count','vpn_tunnel_state','ha_state',
    'trap'
  )
);
