-- Migration 076: widen the canonical alert_rules metric constraint for NetPath
--
-- Supersedes migrate-073. Per that file's contract ("Adding a new metric means
-- a new migration that supersedes this one, never an edit here"), this migration
-- restores the full metric list verbatim and appends the NetPath metric keys.
--
-- NetPath metrics (evaluated by netpath_alert_service):
--   netpath_rtt          end-to-end latency to the target (ms), raise/resolve
--   netpath_loss         end-to-end packet loss to the target (%), raise/resolve
--   netpath_hop_count    number of hops on the path, raise/resolve
--   netpath_path_change  path topology changed (event-driven on/off)
--   netpath_unreachable  target became unreachable (event-driven on/off)
--
-- alert_rules is owned by the postgres superuser, so this file must be applied
-- by a superuser (the app `zenplus` role cannot ALTER it). It is constraint-only
-- (creates no objects, inserts no rows) and therefore always re-applies cleanly.
--
-- Idempotent and safe to re-run.

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
      'apm_slo_burn','apm_synthetic_down','apm_anomaly',
      -- user device tracker (UDT) metrics
      'udt_new_endpoint','udt_rogue_endpoint','udt_watch_endpoint','udt_endpoint_moved',
      'udt_port_capacity_pct',
      -- NetPath (WAN/Internet path) metrics
      'netpath_rtt','netpath_loss','netpath_hop_count',
      'netpath_path_change','netpath_unreachable'
    )
    -- monitoring-template metrics: any series key emitted by a template
    OR metric LIKE 'tpl\_%'
  );

COMMIT;
