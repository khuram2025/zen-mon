-- Migration 030: Server & agent monitoring foundation (Postgres)
--
-- The servers/agents code (server/app/api/v1/servers.py, agents.py) has been
-- shipping since v1.2.23, but the tables were only ever created by hand on the
-- build box.  This migration makes fresh installs work, and adds the new
-- software-baseline (compliance) tables, per-server health reasons, and a
-- server_id column on alerts so server-scoped alerts are first-class.
--
-- Idempotent: safe to run on boxes where the monitoring tables already exist.

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Core fleet tables ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS servers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name varchar(255) NOT NULL,
    hostname varchar(255),
    fqdn varchar(255),
    primary_ip inet,
    site_id uuid REFERENCES sites(id) ON DELETE SET NULL,
    device_id uuid REFERENCES devices(id) ON DELETE SET NULL,
    os_type varchar(20) NOT NULL DEFAULT 'unknown'
        CONSTRAINT servers_os_type_check CHECK (os_type IN ('windows','linux','macos','bsd','other','unknown')),
    os_name varchar(255),
    os_version varchar(128),
    kernel_or_build varchar(128),
    architecture varchar(32),
    collection_mode varchar(20) NOT NULL DEFAULT 'agent'
        CONSTRAINT servers_collection_mode_check CHECK (collection_mode IN ('agent','agentless_wmi','agentless_winrm','snmp','ssh','none')),
    status varchar(20) NOT NULL DEFAULT 'unknown'
        CONSTRAINT servers_status_check CHECK (status IN ('healthy','warning','critical','unknown','stale','disabled')),
    environment varchar(64),
    owner varchar(255),
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_seen timestamptz,
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_servers_status ON servers (status);
CREATE INDEX IF NOT EXISTS idx_servers_site ON servers (site_id);
CREATE INDEX IF NOT EXISTS idx_servers_os_type ON servers (os_type);
CREATE INDEX IF NOT EXISTS idx_servers_collection ON servers (collection_mode);
CREATE INDEX IF NOT EXISTS idx_servers_hostname ON servers (hostname);
CREATE INDEX IF NOT EXISTS idx_servers_last_seen ON servers (last_seen);

CREATE TABLE IF NOT EXISTS agent_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(255) NOT NULL UNIQUE,
    description text,
    platform varchar(20) NOT NULL DEFAULT 'windows'
        CONSTRAINT agent_policies_platform_check CHECK (platform IN ('windows','linux','any')),
    metric_interval_s integer NOT NULL DEFAULT 30,
    upload_interval_s integer NOT NULL DEFAULT 60,
    process_top_n integer NOT NULL DEFAULT 25,
    service_watchlist jsonb NOT NULL DEFAULT '[]'::jsonb,
    process_watchlist jsonb NOT NULL DEFAULT '[]'::jsonb,
    event_log_filters jsonb NOT NULL DEFAULT '[]'::jsonb,
    disk_ignore jsonb NOT NULL DEFAULT '[]'::jsonb,
    network_ignore jsonb NOT NULL DEFAULT '[]'::jsonb,
    cardinality_limits jsonb NOT NULL DEFAULT '{}'::jsonb,
    update_ring varchar(20) NOT NULL DEFAULT 'stable'
        CONSTRAINT agent_policies_update_ring_check CHECK (update_ring IN ('canary','beta','stable','pinned')),
    feature_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_version integer NOT NULL DEFAULT 1,
    is_builtin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    server_id uuid REFERENCES servers(id) ON DELETE SET NULL,
    agent_uid varchar(128) NOT NULL UNIQUE,
    hostname varchar(255),
    platform varchar(20) NOT NULL DEFAULT 'windows'
        CONSTRAINT agents_platform_check CHECK (platform IN ('windows','linux','macos','other')),
    version varchar(32),
    install_id varchar(128),
    site_id uuid REFERENCES sites(id) ON DELETE SET NULL,
    policy_id uuid REFERENCES agent_policies(id) ON DELETE SET NULL,
    status varchar(20) NOT NULL DEFAULT 'enrolling'
        CONSTRAINT agents_status_check CHECK (status IN ('enrolling','online','stale','offline','disabled','updating','error')),
    api_key_hash varchar(128),
    api_key_prefix varchar(16),
    api_key_rotated_at timestamptz,
    last_heartbeat_at timestamptz,
    last_metric_at timestamptz,
    last_config_hash varchar(64),
    config_apply_error text,
    queue_depth integer NOT NULL DEFAULT 0,
    spool_bytes bigint NOT NULL DEFAULT 0,
    update_ring varchar(20) NOT NULL DEFAULT 'stable'
        CONSTRAINT agents_update_ring_check CHECK (update_ring IN ('canary','beta','stable','pinned')),
    desired_version varchar(32),
    current_version varchar(32),
    certificate_expires_at timestamptz,
    last_ip inet,
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agents_server ON agents (server_id);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents (status);
CREATE INDEX IF NOT EXISTS idx_agents_policy ON agents (policy_id);
CREATE INDEX IF NOT EXISTS idx_agents_ring ON agents (update_ring);
CREATE INDEX IF NOT EXISTS idx_agents_apikey ON agents (api_key_hash) WHERE api_key_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_enrollment_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash varchar(128) NOT NULL UNIQUE,
    token_prefix varchar(16) NOT NULL,
    platform varchar(20) NOT NULL DEFAULT 'windows'
        CONSTRAINT agent_enrollment_tokens_platform_check CHECK (platform IN ('windows','linux','macos','any')),
    site_id uuid REFERENCES sites(id) ON DELETE SET NULL,
    policy_id uuid REFERENCES agent_policies(id) ON DELETE SET NULL,
    server_id uuid REFERENCES servers(id) ON DELETE SET NULL,
    hostname_hint varchar(255),
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    expires_at timestamptz NOT NULL,
    max_uses integer NOT NULL DEFAULT 1,
    uses integer NOT NULL DEFAULT 0,
    consumed_at timestamptz,
    consumed_ip inet,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_tokens_expires ON agent_enrollment_tokens (expires_at);

CREATE TABLE IF NOT EXISTS agent_commands (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    command varchar(40) NOT NULL
        CONSTRAINT agent_commands_command_check CHECK (command IN ('status','collect_now','refresh_config','upload_diagnostics','rotate_certificate','restart_agent','upgrade_agent')),
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(20) NOT NULL DEFAULT 'queued'
        CONSTRAINT agent_commands_status_check CHECK (status IN ('queued','sent','running','succeeded','failed','expired','cancelled')),
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    completed_at timestamptz,
    requested_by uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_commands_agent ON agent_commands (agent_id, status);

CREATE TABLE IF NOT EXISTS agent_command_results (
    command_id uuid PRIMARY KEY REFERENCES agent_commands(id) ON DELETE CASCADE,
    success boolean NOT NULL DEFAULT false,
    output jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_diagnostics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    received_at timestamptz,
    file_name varchar(255),
    file_size bigint,
    sha256 varchar(128),
    storage_path varchar(512),
    status varchar(20) NOT NULL DEFAULT 'requested'
        CONSTRAINT agent_diagnostics_status_check CHECK (status IN ('requested','uploading','received','failed','expired')),
    notes text
);

CREATE INDEX IF NOT EXISTS idx_agent_diag_agent ON agent_diagnostics (agent_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS agent_packages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform varchar(20) NOT NULL
        CONSTRAINT agent_packages_platform_check CHECK (platform IN ('windows','linux','macos')),
    arch varchar(16) NOT NULL DEFAULT 'amd64',
    version varchar(32) NOT NULL,
    channel varchar(20) NOT NULL DEFAULT 'stable'
        CONSTRAINT agent_packages_channel_check CHECK (channel IN ('canary','beta','stable','pinned')),
    file_name varchar(255) NOT NULL,
    file_size bigint NOT NULL,
    sha256 varchar(128) NOT NULL,
    signature text,
    signed_by varchar(255),
    download_path varchar(512) NOT NULL,
    is_latest boolean NOT NULL DEFAULT false,
    released_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, arch, version, channel)
);

CREATE INDEX IF NOT EXISTS idx_agent_pkg_latest ON agent_packages (platform, channel, is_latest);

-- ── Last-known inventory snapshots (one row per resource) ────────────

CREATE TABLE IF NOT EXISTS server_process_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    pid integer NOT NULL,
    name varchar(255) NOT NULL,
    cmdline text,
    user_name varchar(255),
    cpu_pct real,
    memory_bytes bigint,
    started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, pid)
);

CREATE INDEX IF NOT EXISTS idx_server_proc_name ON server_process_inventory (server_id, name);

CREATE TABLE IF NOT EXISTS server_service_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    service_name varchar(255) NOT NULL,
    display_name varchar(255),
    start_mode varchar(32),
    state varchar(32),
    pid integer,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, service_name)
);

CREATE TABLE IF NOT EXISTS server_filesystem_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    mount varchar(255) NOT NULL,
    fs_type varchar(64),
    device varchar(255),
    total_bytes bigint,
    used_bytes bigint,
    free_bytes bigint,
    used_pct real,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, mount)
);

CREATE INDEX IF NOT EXISTS idx_server_fs_used ON server_filesystem_inventory (server_id, used_pct DESC);

CREATE TABLE IF NOT EXISTS server_network_interface_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    if_name varchar(128) NOT NULL,
    mac_address varchar(64),
    ip_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    speed_mbps bigint,
    is_up boolean,
    mtu integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, if_name)
);

CREATE TABLE IF NOT EXISTS server_software_inventory (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    package_name varchar(255) NOT NULL,
    version varchar(128),
    vendor varchar(255),
    install_date timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, package_name)
);

-- updated_at triggers (DROP+CREATE: CREATE TRIGGER has no IF NOT EXISTS)
DROP TRIGGER IF EXISTS servers_updated_at ON servers;
CREATE TRIGGER servers_updated_at BEFORE UPDATE ON servers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS agents_updated_at ON agents;
CREATE TRIGGER agents_updated_at BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS agent_policies_updated_at ON agent_policies;
CREATE TRIGGER agent_policies_updated_at BEFORE UPDATE ON agent_policies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Built-in policies
INSERT INTO agent_policies (name, description, platform, metric_interval_s, upload_interval_s, process_top_n, is_builtin)
VALUES
    ('Windows Baseline',    'Default Windows agent policy: 30s collection, 60s upload, top 25 processes.', 'windows', 30, 60, 25, true),
    ('Windows High Detail', 'Windows policy with 10s collection for noisy or high-value hosts.',           'windows', 10, 30, 50, true),
    ('Linux Baseline',      'Default Linux agent policy: 30s collection, 60s upload.',                     'linux',   30, 60, 25, true)
ON CONFLICT (name) DO NOTHING;

-- ── New in 030: server health reasons ────────────────────────────────
-- Human-readable reasons backing servers.status, e.g.
--   ["Filesystem C: at 89.9% (warning ≥ 85%)", "Watched service W3SVC not found"]

ALTER TABLE servers ADD COLUMN IF NOT EXISTS status_reasons jsonb NOT NULL DEFAULT '[]'::jsonb;

-- ── New in 030: server-scoped alerts ─────────────────────────────────

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS server_id uuid REFERENCES servers(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_alerts_server ON alerts (server_id);

-- ── New in 030: software baselines (compliance) ──────────────────────
-- A baseline declares what software a class of servers must (or must not)
-- run.  Scope = os_type + site + match_tags (all optional, AND semantics;
-- match_tags means "server has every listed tag").

CREATE TABLE IF NOT EXISTS software_baselines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(255) NOT NULL UNIQUE,
    description text,
    enabled boolean NOT NULL DEFAULT true,
    os_type varchar(20)
        CONSTRAINT software_baselines_os_type_check CHECK (os_type IS NULL OR os_type IN ('windows','linux','macos','bsd','other')),
    site_id uuid REFERENCES sites(id) ON DELETE SET NULL,
    match_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    alerting boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL
);

DROP TRIGGER IF EXISTS software_baselines_updated_at ON software_baselines;
CREATE TRIGGER software_baselines_updated_at BEFORE UPDATE ON software_baselines
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TABLE IF NOT EXISTS software_baseline_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    baseline_id uuid NOT NULL REFERENCES software_baselines(id) ON DELETE CASCADE,
    rule_type varchar(20) NOT NULL DEFAULT 'required'
        CONSTRAINT sbr_rule_type_check CHECK (rule_type IN ('required','prohibited')),
    package_match varchar(255) NOT NULL,
    match_type varchar(20) NOT NULL DEFAULT 'contains'
        CONSTRAINT sbr_match_type_check CHECK (match_type IN ('exact','contains','regex')),
    min_version varchar(128),
    severity varchar(20) NOT NULL DEFAULT 'warning'
        CONSTRAINT sbr_severity_check CHECK (severity IN ('info','warning','critical')),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sbr_baseline ON software_baseline_rules (baseline_id);

-- Latest evaluation outcome per (server, rule).
CREATE TABLE IF NOT EXISTS server_baseline_results (
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    rule_id uuid NOT NULL REFERENCES software_baseline_rules(id) ON DELETE CASCADE,
    baseline_id uuid NOT NULL REFERENCES software_baselines(id) ON DELETE CASCADE,
    status varchar(20) NOT NULL
        CONSTRAINT sbres_status_check CHECK (status IN ('compliant','missing','outdated','prohibited')),
    found_package varchar(255),
    found_version varchar(128),
    expected text,
    severity varchar(20) NOT NULL DEFAULT 'warning',
    first_failed_at timestamptz,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_sbres_baseline ON server_baseline_results (baseline_id, status);
CREATE INDEX IF NOT EXISTS idx_sbres_server ON server_baseline_results (server_id, status);
