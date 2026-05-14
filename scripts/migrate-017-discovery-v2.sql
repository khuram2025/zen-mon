-- Migration 017: Discovery v2 — Profiles, Schedules, Runs, Results, Rules, Imports, Ignored.
-- Builds the full Discovery Scan module on top of the legacy discovery_jobs/results tables.
-- Idempotent and safe to re-run.

-- ───────────────────────────────────────────────────────────────────────────
-- Discovery profiles
-- A profile bundles scope + credentials + protocols + classification rules.
-- One profile can have many runs, an optional active schedule, and rules.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(150) NOT NULL,
    description     TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,

    -- Scope (Step 1)
    scope_type      VARCHAR(20) NOT NULL DEFAULT 'cidr'
                    CHECK (scope_type IN ('single_ip','ip_range','cidr','multi','csv')),
    targets         JSONB NOT NULL DEFAULT '[]'::jsonb,
    exclusions      JSONB NOT NULL DEFAULT '[]'::jsonb,
    collector_id    VARCHAR(80),

    -- Credentials & Protocols (Step 2)
    protocols       JSONB NOT NULL DEFAULT '["icmp"]'::jsonb,
    custom_ports    JSONB NOT NULL DEFAULT '[]'::jsonb,
    snmp_credential_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    detect_lldp     BOOLEAN NOT NULL DEFAULT TRUE,
    detect_mac      BOOLEAN NOT NULL DEFAULT TRUE,
    detect_vendor   BOOLEAN NOT NULL DEFAULT TRUE,

    -- Performance & safety
    max_concurrency INTEGER NOT NULL DEFAULT 32,
    scan_timeout_ms INTEGER NOT NULL DEFAULT 2000,
    retry_count     INTEGER NOT NULL DEFAULT 1,
    rate_limit_pps  INTEGER NOT NULL DEFAULT 200,
    max_duration_sec INTEGER NOT NULL DEFAULT 1800,

    -- Classification & Import rules (Step 4)
    import_mode     VARCHAR(20) NOT NULL DEFAULT 'review'
                    CHECK (import_mode IN ('review','auto_match','ignore_match')),
    default_group_id UUID REFERENCES device_groups(id) ON DELETE SET NULL,
    default_tags    JSONB NOT NULL DEFAULT '[]'::jsonb,
    default_template_id UUID REFERENCES device_profiles(id) ON DELETE SET NULL,
    default_location TEXT,
    default_owner   TEXT,
    enable_monitoring BOOLEAN NOT NULL DEFAULT TRUE,
    keep_disabled   BOOLEAN NOT NULL DEFAULT FALSE,
    notify_recipients JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Bookkeeping
    last_run_id     UUID,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(name)
);

CREATE INDEX IF NOT EXISTS idx_discovery_profiles_enabled
    ON discovery_profiles(enabled, updated_at DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- Discovery schedules
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id      UUID NOT NULL REFERENCES discovery_profiles(id) ON DELETE CASCADE,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_type   VARCHAR(20) NOT NULL
                    CHECK (schedule_type IN ('once_now','once_future','recurring','cron')),
    frequency       VARCHAR(20)
                    CHECK (frequency IN ('hourly','daily','weekly','monthly','custom')),
    cron_expression VARCHAR(120),
    interval_minutes INTEGER,
    time_of_day     TIME,
    day_of_week     INTEGER,
    day_of_month    INTEGER,
    timezone        VARCHAR(60) NOT NULL DEFAULT 'UTC',
    start_date      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_date        TIMESTAMPTZ,
    maintenance_window JSONB,
    next_run_at     TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,
    last_run_id     UUID,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_schedules_next_run
    ON discovery_schedules(enabled, next_run_at) WHERE enabled = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_discovery_schedule_per_profile
    ON discovery_schedules(profile_id) WHERE enabled = TRUE;


-- ───────────────────────────────────────────────────────────────────────────
-- Discovery runs
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id      UUID NOT NULL REFERENCES discovery_profiles(id) ON DELETE CASCADE,
    schedule_id     UUID REFERENCES discovery_schedules(id) ON DELETE SET NULL,
    trigger_type    VARCHAR(20) NOT NULL
                    CHECK (trigger_type IN ('manual','scheduled','api','retry')),
    status          VARCHAR(20) NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed','cancelled','partial')),
    phase           VARCHAR(40) NOT NULL DEFAULT 'preparing',
    progress_pct    INTEGER NOT NULL DEFAULT 0,

    -- Counts
    total_targets   INTEGER NOT NULL DEFAULT 0,
    completed_targets INTEGER NOT NULL DEFAULT 0,
    responding_targets INTEGER NOT NULL DEFAULT 0,
    failed_targets  INTEGER NOT NULL DEFAULT 0,
    new_devices     INTEGER NOT NULL DEFAULT 0,
    existing_devices INTEGER NOT NULL DEFAULT 0,
    changed_devices INTEGER NOT NULL DEFAULT 0,
    unknown_devices INTEGER NOT NULL DEFAULT 0,
    ignored_devices INTEGER NOT NULL DEFAULT 0,
    credential_failures INTEGER NOT NULL DEFAULT 0,
    duplicate_candidates INTEGER NOT NULL DEFAULT 0,
    ready_to_import INTEGER NOT NULL DEFAULT 0,

    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    activity_log    JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_details   TEXT,

    started_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_profile
    ON discovery_runs(profile_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_status
    ON discovery_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_schedule
    ON discovery_runs(schedule_id, created_at DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- Discovery results v2
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_results_v2 (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
    profile_id      UUID NOT NULL REFERENCES discovery_profiles(id) ON DELETE CASCADE,

    ip_address      INET NOT NULL,
    mac_address     VARCHAR(32),
    hostname        VARCHAR(255),
    fqdn            VARCHAR(255),
    sys_name        VARCHAR(255),
    sys_object_id   VARCHAR(255),
    serial_number   VARCHAR(255),

    vendor          VARCHAR(150),
    device_type     VARCHAR(60),
    model           VARCHAR(150),
    os              VARCHAR(150),
    os_version      VARCHAR(60),

    protocols_detected JSONB NOT NULL DEFAULT '[]'::jsonb,
    open_ports      JSONB NOT NULL DEFAULT '[]'::jsonb,
    response_time_ms INTEGER,

    credential_status VARCHAR(20) NOT NULL DEFAULT 'not_tested'
                    CHECK (credential_status IN ('valid','invalid','not_tested','partial','permission_issue')),
    credential_used UUID REFERENCES snmp_credentials(id) ON DELETE SET NULL,

    status          VARCHAR(20) NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('new','existing','changed','unknown','ignored','failed','imported')),
    matched_device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    matched_template_id UUID REFERENCES device_profiles(id) ON DELETE SET NULL,
    suggested_group_id UUID REFERENCES device_groups(id) ON DELETE SET NULL,
    suggested_tags  JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_score INTEGER NOT NULL DEFAULT 0,

    conflict_type   VARCHAR(40),
    conflict_with_id UUID REFERENCES devices(id) ON DELETE SET NULL,

    import_ready    BOOLEAN NOT NULL DEFAULT FALSE,
    imported        BOOLEAN NOT NULL DEFAULT FALSE,
    imported_at     TIMESTAMPTZ,
    imported_device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    ignored         BOOLEAN NOT NULL DEFAULT FALSE,
    ignored_at      TIMESTAMPTZ,

    error_message   TEXT,
    raw_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_results_v2_run
    ON discovery_results_v2(run_id, status);
CREATE INDEX IF NOT EXISTS idx_discovery_results_v2_status
    ON discovery_results_v2(status, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_results_v2_ip
    ON discovery_results_v2(ip_address);


-- ───────────────────────────────────────────────────────────────────────────
-- Discovery rules
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id      UUID NOT NULL REFERENCES discovery_profiles(id) ON DELETE CASCADE,
    name            VARCHAR(150) NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    match_mode      VARCHAR(10) NOT NULL DEFAULT 'all'
                    CHECK (match_mode IN ('all','any')),
    conditions      JSONB NOT NULL DEFAULT '[]'::jsonb,
    action          VARCHAR(20) NOT NULL
                    CHECK (action IN ('import','ignore','tag','assign')),
    action_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_rules_profile
    ON discovery_rules(profile_id, priority);


-- ───────────────────────────────────────────────────────────────────────────
-- Ignored devices
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_ignored_devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ip_address      INET,
    mac_address     VARCHAR(32),
    hostname        VARCHAR(255),
    reason          TEXT,
    ignored_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    ignored_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(ip_address)
);


-- ───────────────────────────────────────────────────────────────────────────
-- Import batches & items
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discovery_import_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
    profile_id      UUID NOT NULL REFERENCES discovery_profiles(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed','partial')),
    total_items     INTEGER NOT NULL DEFAULT 0,
    successful_items INTEGER NOT NULL DEFAULT 0,
    failed_items    INTEGER NOT NULL DEFAULT 0,
    skipped_items   INTEGER NOT NULL DEFAULT 0,
    group_id        UUID REFERENCES device_groups(id) ON DELETE SET NULL,
    template_id     UUID REFERENCES device_profiles(id) ON DELETE SET NULL,
    snmp_credential_id UUID REFERENCES snmp_credentials(id) ON DELETE SET NULL,
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    enable_monitoring BOOLEAN NOT NULL DEFAULT TRUE,
    started_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    error_details   TEXT
);

CREATE INDEX IF NOT EXISTS idx_discovery_import_batches_run
    ON discovery_import_batches(run_id, started_at DESC);


CREATE TABLE IF NOT EXISTS discovery_import_items (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        UUID NOT NULL REFERENCES discovery_import_batches(id) ON DELETE CASCADE,
    result_id       BIGINT NOT NULL REFERENCES discovery_results_v2(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','imported','skipped','failed','conflict')),
    device_id       UUID REFERENCES devices(id) ON DELETE SET NULL,
    conflict_type   VARCHAR(40),
    error_message   TEXT,
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_discovery_import_items_batch
    ON discovery_import_items(batch_id, status);


-- ───────────────────────────────────────────────────────────────────────────
-- GRANTs for zenplus role
-- ───────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT ALL ON
            discovery_profiles,
            discovery_schedules,
            discovery_runs,
            discovery_results_v2,
            discovery_rules,
            discovery_ignored_devices,
            discovery_import_batches,
            discovery_import_items
        TO zenplus;

        GRANT USAGE, SELECT, UPDATE ON SEQUENCE
            discovery_results_v2_id_seq,
            discovery_import_items_id_seq
        TO zenplus;
    END IF;
END $$;
