-- Migration 016 — Server monitoring (host agents + agentless servers)
--
-- Introduces the "server" entity (monitored host), agent fleet, agent policies,
-- enrollment tokens, package manifest, command queue, and inventory snapshots.
--
-- A "server" is a monitored host distinct from a network "device". A server may
-- be linked to an existing device row through server.device_id, but the
-- agent-managed host metrics live under the new tables here.
--
-- All idempotent: uses IF NOT EXISTS / DO blocks so re-runs are safe.

-- ─── Servers ───
CREATE TABLE IF NOT EXISTS servers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name        VARCHAR(255) NOT NULL,
    hostname            VARCHAR(255),
    fqdn                VARCHAR(255),
    primary_ip          INET,
    site_id             UUID REFERENCES sites(id) ON DELETE SET NULL,
    device_id           UUID REFERENCES devices(id) ON DELETE SET NULL,
    os_type             VARCHAR(20) NOT NULL DEFAULT 'unknown'
                        CHECK (os_type IN ('windows','linux','macos','bsd','other','unknown')),
    os_name             VARCHAR(255),
    os_version          VARCHAR(128),
    kernel_or_build     VARCHAR(128),
    architecture        VARCHAR(32),
    collection_mode     VARCHAR(20) NOT NULL DEFAULT 'agent'
                        CHECK (collection_mode IN ('agent','agentless_wmi','agentless_winrm','snmp','ssh','none')),
    status              VARCHAR(20) NOT NULL DEFAULT 'unknown'
                        CHECK (status IN ('healthy','warning','critical','unknown','stale','disabled')),
    environment         VARCHAR(64),
    owner               VARCHAR(255),
    tags                JSONB NOT NULL DEFAULT '[]',
    last_seen           TIMESTAMPTZ,
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_servers_status         ON servers(status);
CREATE INDEX IF NOT EXISTS idx_servers_os_type        ON servers(os_type);
CREATE INDEX IF NOT EXISTS idx_servers_site           ON servers(site_id);
CREATE INDEX IF NOT EXISTS idx_servers_collection     ON servers(collection_mode);
CREATE INDEX IF NOT EXISTS idx_servers_hostname       ON servers(hostname);
CREATE INDEX IF NOT EXISTS idx_servers_last_seen      ON servers(last_seen);

-- ─── Server tags (auxiliary lookup table for fast filter) ───
CREATE TABLE IF NOT EXISTS server_tags (
    server_id   UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    tag_key     VARCHAR(64) NOT NULL,
    tag_value   VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, tag_key)
);
CREATE INDEX IF NOT EXISTS idx_server_tags_kv ON server_tags(tag_key, tag_value);

-- ─── Agent policies ───
CREATE TABLE IF NOT EXISTS agent_policies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(255) NOT NULL UNIQUE,
    description         TEXT,
    platform            VARCHAR(20) NOT NULL DEFAULT 'windows'
                        CHECK (platform IN ('windows','linux','any')),
    -- collection intervals (seconds)
    metric_interval_s   INTEGER NOT NULL DEFAULT 30,
    upload_interval_s   INTEGER NOT NULL DEFAULT 60,
    process_top_n       INTEGER NOT NULL DEFAULT 25,
    service_watchlist   JSONB NOT NULL DEFAULT '[]',
    process_watchlist   JSONB NOT NULL DEFAULT '[]',
    event_log_filters   JSONB NOT NULL DEFAULT '[]',
    disk_ignore         JSONB NOT NULL DEFAULT '[]',
    network_ignore      JSONB NOT NULL DEFAULT '[]',
    cardinality_limits  JSONB NOT NULL DEFAULT '{}',
    update_ring         VARCHAR(20) NOT NULL DEFAULT 'stable'
                        CHECK (update_ring IN ('canary','beta','stable','pinned')),
    feature_flags       JSONB NOT NULL DEFAULT '{}',
    config_version      INTEGER NOT NULL DEFAULT 1,
    is_builtin          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL
);

-- ─── Agents (one row per installed agent) ───
CREATE TABLE IF NOT EXISTS agents (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_id               UUID REFERENCES servers(id) ON DELETE SET NULL,
    agent_uid               VARCHAR(128) NOT NULL UNIQUE,        -- stable per-install identifier
    hostname                VARCHAR(255),
    platform                VARCHAR(20) NOT NULL DEFAULT 'windows'
                            CHECK (platform IN ('windows','linux','macos','other')),
    version                 VARCHAR(32),
    install_id              VARCHAR(128),
    site_id                 UUID REFERENCES sites(id) ON DELETE SET NULL,
    policy_id               UUID REFERENCES agent_policies(id) ON DELETE SET NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'enrolling'
                            CHECK (status IN ('enrolling','online','stale','offline','disabled','updating','error')),
    -- Credentials
    api_key_hash            VARCHAR(128),
    api_key_prefix          VARCHAR(16),
    api_key_rotated_at      TIMESTAMPTZ,
    -- Health
    last_heartbeat_at       TIMESTAMPTZ,
    last_metric_at          TIMESTAMPTZ,
    last_config_hash        VARCHAR(64),
    config_apply_error      TEXT,
    queue_depth             INTEGER NOT NULL DEFAULT 0,
    spool_bytes             BIGINT NOT NULL DEFAULT 0,
    -- Update rings
    update_ring             VARCHAR(20) NOT NULL DEFAULT 'stable'
                            CHECK (update_ring IN ('canary','beta','stable','pinned')),
    desired_version         VARCHAR(32),
    current_version         VARCHAR(32),
    certificate_expires_at  TIMESTAMPTZ,
    last_ip                 INET,
    -- Misc
    tags                    JSONB NOT NULL DEFAULT '[]',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agents_server   ON agents(server_id);
CREATE INDEX IF NOT EXISTS idx_agents_status   ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_policy   ON agents(policy_id);
CREATE INDEX IF NOT EXISTS idx_agents_apikey   ON agents(api_key_hash) WHERE api_key_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agents_ring     ON agents(update_ring);

-- ─── Agent policy assignments (target type → policy) ───
CREATE TABLE IF NOT EXISTS agent_policy_assignments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id   UUID NOT NULL REFERENCES agent_policies(id) ON DELETE CASCADE,
    target_type VARCHAR(20) NOT NULL CHECK (target_type IN ('agent','server','site','tag','default')),
    target_id   UUID,
    tag_key     VARCHAR(64),
    tag_value   VARCHAR(255),
    priority    INTEGER NOT NULL DEFAULT 100,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_policy_assign_target
    ON agent_policy_assignments(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_agent_policy_assign_policy
    ON agent_policy_assignments(policy_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_policy_assign_unique
    ON agent_policy_assignments(
        policy_id, target_type,
        COALESCE(target_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(tag_key, ''),
        COALESCE(tag_value, '')
    );

-- ─── Enrollment tokens (one-time, hashed at rest) ───
CREATE TABLE IF NOT EXISTS agent_enrollment_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash      VARCHAR(128) NOT NULL UNIQUE,
    token_prefix    VARCHAR(16) NOT NULL,
    platform        VARCHAR(20) NOT NULL DEFAULT 'windows'
                    CHECK (platform IN ('windows','linux','macos','any')),
    site_id         UUID REFERENCES sites(id) ON DELETE SET NULL,
    policy_id       UUID REFERENCES agent_policies(id) ON DELETE SET NULL,
    server_id       UUID REFERENCES servers(id) ON DELETE SET NULL,
    hostname_hint   VARCHAR(255),
    tags            JSONB NOT NULL DEFAULT '[]',
    expires_at      TIMESTAMPTZ NOT NULL,
    max_uses        INTEGER NOT NULL DEFAULT 1,
    uses            INTEGER NOT NULL DEFAULT 0,
    consumed_at     TIMESTAMPTZ,
    consumed_ip     INET,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_expires ON agent_enrollment_tokens(expires_at);

-- ─── Agent packages (the binaries/MSIs we ship) ───
CREATE TABLE IF NOT EXISTS agent_packages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform        VARCHAR(20) NOT NULL
                    CHECK (platform IN ('windows','linux','macos')),
    arch            VARCHAR(16) NOT NULL DEFAULT 'amd64',
    version         VARCHAR(32) NOT NULL,
    channel         VARCHAR(20) NOT NULL DEFAULT 'stable'
                    CHECK (channel IN ('canary','beta','stable','pinned')),
    file_name       VARCHAR(255) NOT NULL,
    file_size       BIGINT NOT NULL,
    sha256          VARCHAR(128) NOT NULL,
    signature       TEXT,
    signed_by       VARCHAR(255),
    download_path   VARCHAR(512) NOT NULL,
    is_latest       BOOLEAN NOT NULL DEFAULT FALSE,
    released_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, arch, version, channel)
);
CREATE INDEX IF NOT EXISTS idx_agent_pkg_latest ON agent_packages(platform, channel, is_latest);

-- ─── Agent commands (safe set: status, collect_now, refresh_config, ...) ───
CREATE TABLE IF NOT EXISTS agent_commands (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    command         VARCHAR(40) NOT NULL
                    CHECK (command IN ('status','collect_now','refresh_config','upload_diagnostics',
                                       'rotate_certificate','restart_agent','upgrade_agent')),
    params          JSONB NOT NULL DEFAULT '{}',
    status          VARCHAR(20) NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','sent','running','succeeded','failed','expired','cancelled')),
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_commands_agent ON agent_commands(agent_id, status);

CREATE TABLE IF NOT EXISTS agent_command_results (
    command_id      UUID PRIMARY KEY REFERENCES agent_commands(id) ON DELETE CASCADE,
    success         BOOLEAN NOT NULL DEFAULT FALSE,
    output          JSONB NOT NULL DEFAULT '{}',
    error_message   TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Agent diagnostics bundle metadata ───
CREATE TABLE IF NOT EXISTS agent_diagnostics (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    received_at     TIMESTAMPTZ,
    file_name       VARCHAR(255),
    file_size       BIGINT,
    sha256          VARCHAR(128),
    storage_path    VARCHAR(512),
    status          VARCHAR(20) NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested','uploading','received','failed','expired')),
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_diag_agent ON agent_diagnostics(agent_id, requested_at DESC);

-- ─── Server inventory snapshots (current values stored in Postgres) ───
CREATE TABLE IF NOT EXISTS server_service_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    service_name  VARCHAR(255) NOT NULL,
    display_name  VARCHAR(255),
    start_mode    VARCHAR(32),
    state         VARCHAR(32),
    pid           INTEGER,
    description   TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, service_name)
);

CREATE TABLE IF NOT EXISTS server_process_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    pid           INTEGER NOT NULL,
    name          VARCHAR(255) NOT NULL,
    cmdline       TEXT,
    user_name     VARCHAR(255),
    cpu_pct       REAL,
    memory_bytes  BIGINT,
    started_at    TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, pid)
);
CREATE INDEX IF NOT EXISTS idx_server_proc_name ON server_process_inventory(server_id, name);

CREATE TABLE IF NOT EXISTS server_filesystem_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    mount         VARCHAR(255) NOT NULL,
    fs_type       VARCHAR(64),
    device        VARCHAR(255),
    total_bytes   BIGINT,
    used_bytes    BIGINT,
    free_bytes    BIGINT,
    used_pct      REAL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, mount)
);
CREATE INDEX IF NOT EXISTS idx_server_fs_used ON server_filesystem_inventory(server_id, used_pct DESC);

CREATE TABLE IF NOT EXISTS server_network_interface_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    if_name       VARCHAR(128) NOT NULL,
    mac_address   VARCHAR(64),
    ip_addresses  JSONB NOT NULL DEFAULT '[]',
    speed_mbps    BIGINT,
    is_up         BOOLEAN,
    mtu           INTEGER,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, if_name)
);

CREATE TABLE IF NOT EXISTS server_software_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    package_name  VARCHAR(255) NOT NULL,
    version       VARCHAR(128),
    vendor        VARCHAR(255),
    install_date  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, package_name)
);

-- ─── Server credentials (agentless WMI/WinRM/SSH) ───
CREATE TABLE IF NOT EXISTS server_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    cred_type       VARCHAR(20) NOT NULL
                    CHECK (cred_type IN ('wmi','winrm','ssh_password','ssh_key','snmp')),
    username        VARCHAR(255),
    domain          VARCHAR(255),
    secret_cipher   BYTEA,           -- encrypted credential blob
    secret_kid      VARCHAR(64),     -- key id for rotation
    site_id         UUID REFERENCES sites(id) ON DELETE SET NULL,
    tags            JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

-- ─── updated_at triggers ───
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'servers_updated_at') THEN
        CREATE TRIGGER servers_updated_at BEFORE UPDATE ON servers
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agents_updated_at') THEN
        CREATE TRIGGER agents_updated_at BEFORE UPDATE ON agents
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agent_policies_updated_at') THEN
        CREATE TRIGGER agent_policies_updated_at BEFORE UPDATE ON agent_policies
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'server_credentials_updated_at') THEN
        CREATE TRIGGER server_credentials_updated_at BEFORE UPDATE ON server_credentials
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;

-- ─── Seed built-in policies ───
INSERT INTO agent_policies (name, description, platform, metric_interval_s, upload_interval_s,
                            process_top_n, is_builtin, config_version)
VALUES
  ('Windows Baseline',     'Default Windows agent policy: 30s collection, 60s upload, top 25 processes.',
   'windows', 30, 60, 25, TRUE, 1),
  ('Windows High Detail',  'Windows policy with 10s collection for noisy or high-value hosts.',
   'windows', 10, 30, 50, TRUE, 1),
  ('Linux Baseline',       'Default Linux agent policy: 30s collection, 60s upload.',
   'linux',   30, 60, 25, TRUE, 1)
ON CONFLICT (name) DO NOTHING;

-- ─── Grants ───
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            servers, server_tags, agents, agent_policies, agent_policy_assignments,
            agent_enrollment_tokens, agent_packages, agent_commands, agent_command_results,
            agent_diagnostics, server_service_inventory, server_process_inventory,
            server_filesystem_inventory, server_network_interface_inventory,
            server_software_inventory, server_credentials
        TO zenplus;
    END IF;
END $$;
