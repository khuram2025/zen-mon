-- Restore the final alert metric constraint after releases that may have
-- replayed an older, narrower constraint definition.
--
-- This migration is intentionally forward-only and idempotent. Keep the
-- complete metric list here so existing network and APM rules remain valid.

BEGIN;

ALTER TABLE alert_rules
  DROP CONSTRAINT IF EXISTS alert_rules_metric_check;

ALTER TABLE alert_rules
  ADD CONSTRAINT alert_rules_metric_check CHECK (
    metric IN (
      'ping_status','rtt','packet_loss','jitter','service_status',
      'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
      'if_in_bps','if_out_bps','if_util_pct','if_errors','if_discards','if_oper_status',
      'session_count','vpn_tunnel_state','ha_state','bgp_neighbor_down',
      'trap',
      -- host (server agent) metrics
      'host_cpu_pct','host_memory_pct','host_filesystem_pct','host_disk_util_pct',
      'host_service_down','host_process_down',
      -- application monitoring (APM) metrics
      'apm_latency_p50','apm_latency_p95','apm_latency_p99',
      'apm_error_rate','apm_throughput','apm_apdex',
      'apm_slo_burn','apm_synthetic_down','apm_anomaly'
    )
  );

COMMIT;
