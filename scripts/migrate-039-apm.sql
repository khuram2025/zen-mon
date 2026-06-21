-- migrate-039-apm.sql
-- ZenPlus Application Monitoring (APM) — PostgreSQL configuration model.
--
-- All APM *config/registry/triage* state (never high-volume telemetry, which
-- lives in ClickHouse). Idempotent: every statement is CREATE ... IF NOT EXISTS.
-- One PG migration covers v1 (AM-E1 ingest keys + AM-E3 services + AM-E6 SLOs).
--
-- Also re-declares the alert_rules.metric CHECK constraint (full list, in lockstep
-- with migrate-035/037) with the nine apm_* metric keys appended so APM signals
-- plug into the existing multi-condition alert engine.
--
-- House style (per migrate-038): UUID PK gen_random_uuid() (pgcrypto), TIMESTAMPTZ
-- DEFAULT NOW(), JSONB DEFAULT, VARCHAR CHECK enums, GIN on queried JSONB.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─────────────────────────────────────────────────────────────────────────────
-- Environments
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL UNIQUE,
    retention_days_raw SMALLINT NOT NULL DEFAULT 7 CHECK (retention_days_raw BETWEEN 1 AND 30),
    sampling_target_tps SMALLINT NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO apm_environments (name) VALUES ('prod'), ('staging'), ('dev')
ON CONFLICT (name) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- Service registry (auto-registered on first span; denormalized last-seen RED)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    language VARCHAR(32),
    team VARCHAR(128),
    owner VARCHAR(128),
    repo_url TEXT,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    health VARCHAR(16) NOT NULL DEFAULT 'no_data'
           CHECK (health IN ('healthy','degraded','critical','no_data')),
    apdex_threshold_ms INTEGER NOT NULL DEFAULT 500,
    last_seen_at TIMESTAMPTZ,
    last_rps DOUBLE PRECISION,
    last_error_rate DOUBLE PRECISION,
    last_p95_ms DOUBLE PRECISION,
    last_apdex DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (name, env_id)
);
CREATE INDEX IF NOT EXISTS idx_apm_services_health ON apm_services(health);
CREATE INDEX IF NOT EXISTS idx_apm_services_tags ON apm_services USING GIN (tags);

-- ─────────────────────────────────────────────────────────────────────────────
-- Ingest keys (zpi_ for SDK, zpr_ for RUM); plaintext shown once, hash stored
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_ingest_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'sdk' CHECK (kind IN ('sdk','rum')),
    key_hash VARCHAR(128) NOT NULL UNIQUE,
    key_prefix VARCHAR(16) NOT NULL,
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    origin_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apm_ingest_keys_hash ON apm_ingest_keys(key_hash) WHERE revoked_at IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Enrollment tokens (mirrors agent_enrollment_tokens)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_enrollment_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    token_prefix VARCHAR(16) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'sdk' CHECK (kind IN ('sdk','rum')),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    consumed_ip INET,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SLOs
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_slos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    operation VARCHAR(255),
    sli_type VARCHAR(32) NOT NULL CHECK (sli_type IN ('availability','latency','error_rate','custom')),
    latency_threshold_ms INTEGER,
    target DOUBLE PRECISION NOT NULL,
    window_days SMALLINT NOT NULL DEFAULT 30 CHECK (window_days IN (7,30,90)),
    burn_alert_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notify_channels JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apm_slos_service ON apm_slos(service_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Synthetic monitors (reuses the service_checks JSONB config idiom)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_synthetic_monitors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    monitor_type VARCHAR(16) NOT NULL DEFAULT 'api' CHECK (monitor_type IN ('api','browser')),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    target_url TEXT,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,        -- steps / assertions
    locations JSONB NOT NULL DEFAULT '[]'::jsonb,
    check_interval INTEGER NOT NULL DEFAULT 60,
    timeout INTEGER NOT NULL DEFAULT 30,
    retry_count SMALLINT NOT NULL DEFAULT 1,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(16) NOT NULL DEFAULT 'unknown',
    last_check_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Sampling rules (head/tail policy)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_sampling_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    scope VARCHAR(16) NOT NULL DEFAULT 'global' CHECK (scope IN ('global','env','service')),
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    sample_type VARCHAR(8) NOT NULL DEFAULT 'tail' CHECK (sample_type IN ('head','tail')),
    conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
    sample_rate DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (sample_rate BETWEEN 0 AND 1),
    priority SMALLINT NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Scrubbing rules (PII redaction) + default rules
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_scrubbing_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    attribute_key VARCHAR(255),                       -- exact key or pattern
    pattern TEXT,                                      -- regex matched against value
    action VARCHAR(16) NOT NULL DEFAULT 'redact' CHECK (action IN ('redact','hash','drop')),
    scope VARCHAR(16) NOT NULL DEFAULT 'global' CHECK (scope IN ('global','env','service')),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    priority SMALLINT NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO apm_scrubbing_rules (name, pattern, action, scope)
SELECT v.name, v.pattern, 'redact', 'global'
FROM (VALUES
    ('Credit card numbers', '\m(?:\d[ -]*?){13,19}\M'),
    ('Email addresses',     '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    ('Bearer tokens',       '(?i)bearer\s+[A-Za-z0-9._-]+')
) AS v(name, pattern)
WHERE NOT EXISTS (SELECT 1 FROM apm_scrubbing_rules s WHERE s.name = v.name);

-- ─────────────────────────────────────────────────────────────────────────────
-- Deployments / change markers
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    version VARCHAR(128) NOT NULL,
    git_sha VARCHAR(64),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    deployed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apm_deployments_service ON apm_deployments(service_id, deployed_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- Saved dashboards
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_dashboards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    layout JSONB NOT NULL DEFAULT '[]'::jsonb,
    owner UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Error-issue triage state (grouping key lives in CH apm_exceptions.group_id)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS apm_error_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id VARCHAR(32) NOT NULL,
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    status VARCHAR(24) NOT NULL DEFAULT 'unresolved'
           CHECK (status IN ('unresolved','resolved','resolved_in_version','ignored')),
    resolved_in_version VARCHAR(128),
    assignee VARCHAR(128),
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (group_id, service_id)
);
CREATE INDEX IF NOT EXISTS idx_apm_error_issues_status ON apm_error_issues(status);

-- ─────────────────────────────────────────────────────────────────────────────
-- Alert engine integration: re-declare the full metric CHECK + apm_* keys.
-- (Keep the existing keys in lockstep with migrate-035/037.)
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_metric_check;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check CHECK (
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
