-- Migration 073: restore the canonical alert_rules metric constraint
--
-- Nine earlier migrations each drop and recreate alert_rules_metric_check under
-- the same name, widening the allowed metric list a little further every time.
-- That makes the constraint unusable as evidence of which migration has run,
-- and it makes those files non-re-runnable: applying an older one after a newer
-- one has widened the list fails with "check constraint is violated by some
-- row" as soon as any rule uses a metric the older list did not know about.
--
-- A fresh install hit exactly that. install.sh applied every migrate-*.sql
-- blind, then the tracked runner re-applied the ones it could not baseline
-- (they CREATE nothing, so there was nothing to probe), and migrate-022 failed
-- against rows that migrate-035/037 had inserted. Worse, these files carry no
-- transaction wrapper, so the DROP committed and the failed ADD left the table
-- with no metric constraint at all.
--
-- The runner now baselines on tables/columns/indexes and ignores constraints,
-- which stops the re-apply — but an appliance that baselines migrate-060 and
-- migrate-062 never runs their widening either. This migration is the single
-- authoritative definition: it restores the full list on every appliance
-- regardless of which path it took to get here.
--
-- Keep this file as the one place the list is widened from now on. Adding a new
-- metric means a new migration that supersedes this one, never an edit here —
-- editing a shipped migration changes its checksum and strands appliances at
-- the schema gate.
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
      'udt_port_capacity_pct'
    )
    -- monitoring-template metrics: any series key emitted by a template
    OR metric LIKE 'tpl\_%'
  );

COMMIT;
