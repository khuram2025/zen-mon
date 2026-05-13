-- Migration 014: topology links and dependency-aware alerting
-- Stores LLDP/CDP/manual topology edges and parent/child dependencies used
-- to suppress downstream alert storms.

CREATE TABLE IF NOT EXISTS topology_links (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    local_device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    local_if_index      INTEGER,
    local_if_name       VARCHAR(255),
    remote_device_id    UUID REFERENCES devices(id) ON DELETE SET NULL,
    remote_chassis_id   VARCHAR(255),
    remote_port_id      VARCHAR(255),
    remote_hostname     VARCHAR(255),
    remote_if_name      VARCHAR(255),
    protocol            VARCHAR(20) NOT NULL DEFAULT 'manual'
                        CHECK (protocol IN ('lldp', 'cdp', 'manual', 'inferred')),
    confidence          INTEGER NOT NULL DEFAULT 70 CHECK (confidence BETWEEN 0 AND 100),
    source              VARCHAR(80) NOT NULL DEFAULT 'manual',
    metadata            JSONB NOT NULL DEFAULT '{}',
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE topology_links
    ADD COLUMN IF NOT EXISTS local_if_index INTEGER,
    ADD COLUMN IF NOT EXISTS local_if_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS remote_device_id UUID,
    ADD COLUMN IF NOT EXISTS remote_chassis_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS remote_port_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS remote_hostname VARCHAR(255),
    ADD COLUMN IF NOT EXISTS remote_if_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS protocol VARCHAR(20) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS confidence INTEGER NOT NULL DEFAULT 70,
    ADD COLUMN IF NOT EXISTS source VARCHAR(80) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_topology_links_unique_observed
    ON topology_links (
        local_device_id,
        COALESCE(local_if_index, -1),
        protocol,
        COALESCE(remote_chassis_id, ''),
        COALESCE(remote_port_id, ''),
        COALESCE(remote_hostname, '')
    );

CREATE INDEX IF NOT EXISTS idx_topology_links_local_device
    ON topology_links(local_device_id);
CREATE INDEX IF NOT EXISTS idx_topology_links_remote_device
    ON topology_links(remote_device_id);
CREATE INDEX IF NOT EXISTS idx_topology_links_last_seen
    ON topology_links(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS topology_dependencies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    child_device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    dependency_type     VARCHAR(40) NOT NULL DEFAULT 'uplink'
                        CHECK (dependency_type IN ('uplink', 'wan', 'power', 'site', 'service', 'manual')),
    suppress_alerts     BOOLEAN NOT NULL DEFAULT TRUE,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (parent_device_id <> child_device_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_topology_dependencies_unique
    ON topology_dependencies(parent_device_id, child_device_id, dependency_type);
CREATE INDEX IF NOT EXISTS idx_topology_dependencies_parent
    ON topology_dependencies(parent_device_id);
CREATE INDEX IF NOT EXISTS idx_topology_dependencies_child
    ON topology_dependencies(child_device_id);
CREATE INDEX IF NOT EXISTS idx_topology_dependencies_enabled
    ON topology_dependencies(enabled, suppress_alerts);

CREATE TABLE IF NOT EXISTS topology_discovery_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          VARCHAR(20) NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed')),
    protocol_counts JSONB NOT NULL DEFAULT '{}',
    devices_scanned INTEGER NOT NULL DEFAULT 0,
    links_found     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_topology_discovery_runs_started
    ON topology_discovery_runs(started_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON topology_links TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON topology_dependencies TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON topology_discovery_runs TO zenplus;
    END IF;
END $$;
