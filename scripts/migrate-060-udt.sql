-- migrate-060-udt.sql
-- User Device Tracker (UDT): endpoint discovery and switch-port tracking.
--
-- Tables:
--   udt_endpoints           unified endpoint identity, one row per MAC
--   udt_endpoint_locations  sessionized MAC-to-switch-port attachments
--   udt_ip_history          IP<->MAC bindings observed via ARP/ND
--   udt_port_state          per switch-port UDT rollup (uplink flags, capacity)
--   udt_vlans               VLAN inventory per device
--   udt_rules               watch / allow / ignore rule lists
--   udt_user_logins         Windows/AD logon events correlated to endpoints
--   udt_domain_controllers  DCs polled for logon events over WinRM
--   udt_events              UDT activity feed (new endpoint, moves, rogue, watch)
--   udt_oui                 IEEE OUI prefix -> vendor lookup
--   udt_port_capacity_daily daily port-usage snapshots for capacity trending
--
-- Also:
--   devices.auto_rename_from_snmp  -- referenced by the poller's UpsertSystemInfo
--                                     but never created by any migration; added
--                                     here so SNMP system-info writeback works.
--   alert_rules metric CHECK extended with udt_* metrics.

BEGIN;

-- ---------------------------------------------------------------- devices fix
ALTER TABLE devices ADD COLUMN IF NOT EXISTS auto_rename_from_snmp BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------------------------------------------------------------- endpoints
CREATE TABLE IF NOT EXISTS udt_endpoints (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mac            MACADDR NOT NULL UNIQUE,
    vendor         VARCHAR(255),
    hostname       VARCHAR(255),
    ip_address     INET,
    endpoint_type  VARCHAR(30) NOT NULL DEFAULT 'unknown',
    is_randomized  BOOLEAN NOT NULL DEFAULT FALSE,
    device_id      UUID REFERENCES devices(id) ON DELETE SET NULL,
    user_name      VARCHAR(255),
    user_domain    VARCHAR(150),
    user_seen_at   TIMESTAMPTZ,
    is_watched     BOOLEAN NOT NULL DEFAULT FALSE,
    authorized     BOOLEAN,            -- NULL = unclassified, TRUE = allowed, FALSE = rogue
    ignored        BOOLEAN NOT NULL DEFAULT FALSE,
    notes          TEXT,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_ip        ON udt_endpoints (ip_address);
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_hostname  ON udt_endpoints (LOWER(hostname));
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_user      ON udt_endpoints (LOWER(user_name));
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_last_seen ON udt_endpoints (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_watched   ON udt_endpoints (is_watched) WHERE is_watched;
CREATE INDEX IF NOT EXISTS idx_udt_endpoints_rogue     ON udt_endpoints (authorized) WHERE authorized = FALSE;

-- ---------------------------------------------------------------- locations
CREATE TABLE IF NOT EXISTS udt_endpoint_locations (
    id           BIGSERIAL PRIMARY KEY,
    endpoint_id  UUID NOT NULL REFERENCES udt_endpoints(id) ON DELETE CASCADE,
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_index     INTEGER NOT NULL,
    vlan_id      INTEGER,
    is_direct    BOOLEAN NOT NULL DEFAULT TRUE,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at    TIMESTAMPTZ
);
-- one ACTIVE session per endpoint+device+port+vlan
CREATE UNIQUE INDEX IF NOT EXISTS idx_udt_locations_active_unique
    ON udt_endpoint_locations (endpoint_id, device_id, if_index, COALESCE(vlan_id, -1))
    WHERE active;
CREATE INDEX IF NOT EXISTS idx_udt_locations_endpoint  ON udt_endpoint_locations (endpoint_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_udt_locations_port      ON udt_endpoint_locations (device_id, if_index) WHERE active;
CREATE INDEX IF NOT EXISTS idx_udt_locations_stale     ON udt_endpoint_locations (last_seen) WHERE active;

-- ---------------------------------------------------------------- ip history
CREATE TABLE IF NOT EXISTS udt_ip_history (
    id                   BIGSERIAL PRIMARY KEY,
    endpoint_id          UUID NOT NULL REFERENCES udt_endpoints(id) ON DELETE CASCADE,
    ip                   INET NOT NULL,
    source               VARCHAR(20) NOT NULL DEFAULT 'arp',   -- arp | nd | dhcp | manual
    reporting_device_id  UUID REFERENCES devices(id) ON DELETE SET NULL,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_udt_ip_history_active_unique
    ON udt_ip_history (endpoint_id, ip) WHERE active;
CREATE INDEX IF NOT EXISTS idx_udt_ip_history_ip ON udt_ip_history (ip);
CREATE INDEX IF NOT EXISTS idx_udt_ip_history_stale ON udt_ip_history (last_seen) WHERE active;

-- ---------------------------------------------------------------- port state
CREATE TABLE IF NOT EXISTS udt_port_state (
    device_id        UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_index         INTEGER NOT NULL,
    is_uplink        BOOLEAN NOT NULL DEFAULT FALSE,
    uplink_reason    VARCHAR(30),          -- lldp | cdp | trunk | mac_count | manual
    uplink_override  VARCHAR(10) CHECK (uplink_override IN ('uplink', 'access')),
    monitored        BOOLEAN NOT NULL DEFAULT TRUE,
    mac_count        INTEGER NOT NULL DEFAULT 0,
    vlan_ids         JSONB NOT NULL DEFAULT '[]'::jsonb,
    pvid             INTEGER,
    last_endpoint_seen TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, if_index)
);

-- ---------------------------------------------------------------- vlans
CREATE TABLE IF NOT EXISTS udt_vlans (
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    vlan_id     INTEGER NOT NULL,
    name        VARCHAR(255),
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, vlan_id)
);

-- ---------------------------------------------------------------- rules
CREATE TABLE IF NOT EXISTS udt_rules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    list_type    VARCHAR(10) NOT NULL CHECK (list_type IN ('watch', 'allow', 'ignore')),
    match_type   VARCHAR(20) NOT NULL CHECK (match_type IN ('mac', 'mac_prefix', 'ip', 'ip_range', 'subnet', 'hostname', 'vendor', 'user')),
    pattern      VARCHAR(255) NOT NULL,
    description  TEXT,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_udt_rules_type ON udt_rules (list_type) WHERE enabled;

-- ---------------------------------------------------------------- user logins
CREATE TABLE IF NOT EXISTS udt_domain_controllers (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   VARCHAR(150) NOT NULL UNIQUE,
    host                   VARCHAR(255) NOT NULL,
    windows_credential_id  UUID REFERENCES windows_credentials(id) ON DELETE SET NULL,
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    poll_interval_s        INTEGER NOT NULL DEFAULT 300,
    last_poll_at           TIMESTAMPTZ,
    last_status            VARCHAR(20),      -- ok | error | never
    last_error             TEXT,
    last_event_time        TIMESTAMPTZ,      -- checkpoint: newest event consumed
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS udt_user_logins (
    id           BIGSERIAL PRIMARY KEY,
    user_name    VARCHAR(255) NOT NULL,
    user_domain  VARCHAR(150),
    endpoint_id  UUID REFERENCES udt_endpoints(id) ON DELETE SET NULL,
    ip           INET,
    hostname     VARCHAR(255),
    event_id     INTEGER,                -- 4768 | 4769 | 4624
    logon_type   INTEGER,
    dc_id        UUID REFERENCES udt_domain_controllers(id) ON DELETE SET NULL,
    event_time   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_udt_user_logins_user     ON udt_user_logins (LOWER(user_name), event_time DESC);
CREATE INDEX IF NOT EXISTS idx_udt_user_logins_endpoint ON udt_user_logins (endpoint_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_udt_user_logins_time     ON udt_user_logins (event_time DESC);
-- dedupe guard: one row per DC+event identity
CREATE UNIQUE INDEX IF NOT EXISTS idx_udt_user_logins_dedupe
    ON udt_user_logins (dc_id, event_time, user_name, COALESCE(ip, '0.0.0.0'::inet), COALESCE(event_id, 0));

-- ---------------------------------------------------------------- events feed
CREATE TABLE IF NOT EXISTS udt_events (
    id           BIGSERIAL PRIMARY KEY,
    event_type   VARCHAR(30) NOT NULL,   -- new_endpoint | endpoint_moved | rogue_detected | watch_seen | ip_changed | user_login | port_admin
    endpoint_id  UUID REFERENCES udt_endpoints(id) ON DELETE CASCADE,
    device_id    UUID REFERENCES devices(id) ON DELETE SET NULL,
    if_index     INTEGER,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    alerted      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_udt_events_time ON udt_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_udt_events_endpoint ON udt_events (endpoint_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_udt_events_unalerted ON udt_events (id) WHERE NOT alerted;

-- ---------------------------------------------------------------- OUI vendors
CREATE TABLE IF NOT EXISTS udt_oui (
    prefix  VARCHAR(6) PRIMARY KEY,      -- first 6 hex chars of MAC, lowercase, no separators
    vendor  VARCHAR(255) NOT NULL
);

-- ---------------------------------------------------------------- capacity snapshots
CREATE TABLE IF NOT EXISTS udt_port_capacity_daily (
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    day           DATE NOT NULL,
    total_ports   INTEGER NOT NULL DEFAULT 0,
    used_ports    INTEGER NOT NULL DEFAULT 0,   -- oper up
    active_ports  INTEGER NOT NULL DEFAULT 0,   -- endpoint seen within 24h
    uplink_ports  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, day)
);

-- ---------------------------------------------------------------- alert metrics
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
  );

-- ---------------------------------------------------------------- grants
DO $$
DECLARE t TEXT;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    FOREACH t IN ARRAY ARRAY[
      'udt_endpoints', 'udt_endpoint_locations', 'udt_ip_history', 'udt_port_state',
      'udt_vlans', 'udt_rules', 'udt_domain_controllers', 'udt_user_logins',
      'udt_events', 'udt_oui', 'udt_port_capacity_daily'
    ] LOOP
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO zenplus', t);
    END LOOP;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO zenplus;
  END IF;
END $$;

COMMIT;
