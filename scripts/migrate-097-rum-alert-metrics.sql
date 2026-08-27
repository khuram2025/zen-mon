-- Browser RUM alert metrics: widen the canonical alert_rules metric check.
--
-- Supersedes migrate-076. Keep the complete canonical list here so replaying
-- this constraint-only migration never narrows metrics shipped by an earlier
-- subsystem. Idempotent and safe to re-run.

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
      'host_cpu_pct','host_memory_pct','host_filesystem_pct','host_disk_util_pct',
      'host_service_down','host_process_down',
      'apm_latency_p50','apm_latency_p95','apm_latency_p99',
      'apm_error_rate','apm_throughput','apm_apdex',
      'apm_slo_burn','apm_synthetic_down','apm_anomaly',
      'apm_rum_lcp_p75','apm_rum_inp_p75','apm_rum_cls_p75',
      'apm_rum_error_session_rate','apm_rum_resource_failure_rate',
      'udt_new_endpoint','udt_rogue_endpoint','udt_watch_endpoint','udt_endpoint_moved',
      'udt_port_capacity_pct',
      'netpath_rtt','netpath_loss','netpath_hop_count',
      'netpath_path_change','netpath_unreachable'
    )
    OR metric LIKE 'tpl\_%'
  );

COMMIT;
