-- Migration 005: SNMP discovery jobs + MIB storage metadata.
-- Safe to re-run.

-- ─── Discovery jobs ───
CREATE TABLE IF NOT EXISTS discovery_jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cidr           TEXT NOT NULL,                                  -- "10.0.0.0/24"
    community      TEXT,                                           -- v2c community for the sweep
    snmp_version   VARCHAR(5) NOT NULL DEFAULT '2c',
    snmp_port      INTEGER NOT NULL DEFAULT 161,
    timeout_ms     INTEGER NOT NULL DEFAULT 1500,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','completed','failed','cancelled')),
    total_hosts    INTEGER NOT NULL DEFAULT 0,
    scanned_hosts  INTEGER NOT NULL DEFAULT 0,
    responding_hosts INTEGER NOT NULL DEFAULT 0,
    error_message  TEXT,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by     UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_discovery_jobs_status
    ON discovery_jobs(status, created_at DESC);

-- ─── Discovery results (staging area) ───
CREATE TABLE IF NOT EXISTS discovery_results (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL REFERENCES discovery_jobs(id) ON DELETE CASCADE,
    ip_address      INET NOT NULL,
    is_reachable    BOOLEAN NOT NULL DEFAULT FALSE,   -- ICMP
    snmp_responded  BOOLEAN NOT NULL DEFAULT FALSE,
    sys_object_id   TEXT,
    sys_descr       TEXT,
    sys_name        TEXT,
    hostname_guess  TEXT,   -- reverse DNS or sys_name
    matched_profile_id UUID REFERENCES device_profiles(id) ON DELETE SET NULL,
    matched_vendor  TEXT,
    matched_model   TEXT,
    matched_os_version TEXT,
    already_known   BOOLEAN NOT NULL DEFAULT FALSE,   -- set if devices.ip_address exists
    imported        BOOLEAN NOT NULL DEFAULT FALSE,
    imported_device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    error_message   TEXT,
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, ip_address)
);

CREATE INDEX IF NOT EXISTS idx_discovery_results_job
    ON discovery_results(job_id, is_reachable DESC, snmp_responded DESC);

-- ─── MIB library metadata ───
-- Files themselves live on disk under /opt/zenplus/data/mibs/.
-- Compilation (phase 3+ via gosmi) is tracked by `compiled_at`.
CREATE TABLE IF NOT EXISTS snmp_mibs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(255) NOT NULL UNIQUE,   -- e.g. "CISCO-PROCESS-MIB"
    filename     VARCHAR(255) NOT NULL,          -- on-disk name
    size_bytes   BIGINT NOT NULL,
    sha256       VARCHAR(64) NOT NULL,
    uploaded_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    compiled_at  TIMESTAMPTZ,
    compile_error TEXT,
    description  TEXT
);

CREATE INDEX IF NOT EXISTS idx_snmp_mibs_name ON snmp_mibs(name);

-- ─── GRANTs for zenplus role (same rationale as migrate-004) ───
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT ALL ON discovery_jobs, discovery_results, snmp_mibs TO zenplus;
        GRANT USAGE, SELECT, UPDATE ON SEQUENCE discovery_results_id_seq TO zenplus;
    END IF;
END $$;
