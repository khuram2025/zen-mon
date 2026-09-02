-- Migration 108: browser RUM issue lifecycle, source maps, and the
-- "new error group" alert metric (RUM roadmap phase 3).
--
-- Additive and idempotent. Error groups themselves live in ClickHouse
-- (fingerprint per application/env); this table only carries the state an
-- operator sets on a group, so it stays small and survives raw retention.

BEGIN;

CREATE TABLE IF NOT EXISTS rum_issues (
    application_id   VARCHAR(128) NOT NULL,
    env              VARCHAR(64)  NOT NULL DEFAULT '',
    fingerprint      VARCHAR(128) NOT NULL,
    status           VARCHAR(16)  NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'resolved', 'ignored')),
    note             TEXT         NOT NULL DEFAULT '',
    -- Release the group was first observed in, captured when the row is made.
    first_seen_release VARCHAR(128) NOT NULL DEFAULT '',
    -- Set when status becomes 'resolved'; an occurrence after this instant
    -- with a newer release marks the group as regressed on read.
    resolved_at      TIMESTAMPTZ,
    resolved_release VARCHAR(128) NOT NULL DEFAULT '',
    updated_by       VARCHAR(255) NOT NULL DEFAULT '',
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, env, fingerprint)
);

CREATE INDEX IF NOT EXISTS rum_issues_status_idx ON rum_issues (application_id, env, status);

-- Uploaded JavaScript source maps, keyed by the minified file they describe.
-- The map body is stored gzip-compressed; a 5 MB map compresses to ~1 MB.
CREATE TABLE IF NOT EXISTS rum_source_maps (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id   VARCHAR(128) NOT NULL,
    -- Release (service_version) the bundle shipped in; '' applies to any release.
    release          VARCHAR(128) NOT NULL DEFAULT '',
    -- Basename of the minified file the browser loads, e.g. "app.3f2a1c.js".
    file_name        VARCHAR(512) NOT NULL,
    map_gzip         BYTEA        NOT NULL,
    size_bytes       INTEGER      NOT NULL,
    sources_count    INTEGER      NOT NULL DEFAULT 0,
    uploaded_by      VARCHAR(255) NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (application_id, release, file_name)
);

-- Alert on new browser error groups: widen the canonical metric check.
-- Keep the complete list so replaying never narrows earlier subsystems.
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
      'apm_rum_new_error_groups',
      'udt_new_endpoint','udt_rogue_endpoint','udt_watch_endpoint','udt_endpoint_moved',
      'udt_port_capacity_pct',
      'netpath_rtt','netpath_loss','netpath_hop_count',
      'netpath_path_change','netpath_unreachable'
    )
    OR metric LIKE 'tpl\_%'
  );

COMMIT;
