-- Migration 008 — Remote sensors (distributed pollers)
-- Adds tables to support remote sensor VMs that perform health checks at
-- branch sites and push results to central over HTTPS.
--
-- New tables:
--   sites                 — physical/logical locations (branch, DC, region)
--   sensors               — registered remote pollers
--   sensor_assignments    — many-to-many: which sensor monitors which target
--
-- Existing tables touched:
--   devices               — adds default_sensor_id (nullable FK)
--   service_checks        — adds default_sensor_id (nullable FK)
--
-- Idempotent: uses IF NOT EXISTS / IF EXISTS so re-runs are safe.

-- ─── Sites ───
CREATE TABLE IF NOT EXISTS sites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL UNIQUE,
    region      VARCHAR(100),
    timezone    VARCHAR(64) DEFAULT 'UTC',
    address     TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Sensors ───
CREATE TABLE IF NOT EXISTS sensors (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    VARCHAR(255) NOT NULL UNIQUE,
    description             TEXT,
    site_id                 UUID REFERENCES sites(id) ON DELETE SET NULL,
    location                VARCHAR(255),

    -- Auth & enrollment
    enrollment_token_hash   VARCHAR(128),               -- one-time bootstrap, hashed
    enrollment_expires_at   TIMESTAMPTZ,
    enrollment_consumed_at  TIMESTAMPTZ,
    enrollment_consumed_ip  INET,
    api_key_hash            VARCHAR(128),               -- long-lived, hashed (sha256 hex)
    api_key_prefix          VARCHAR(16),                -- first 8 chars for display, e.g. "zps_a3f9..."
    api_key_rotated_at      TIMESTAMPTZ,

    -- Status / runtime
    status                  VARCHAR(20) DEFAULT 'pending'
                            CHECK (status IN ('pending','online','degraded','offline','disabled')),
    version                 VARCHAR(32),
    last_seen_at            TIMESTAMPTZ,
    last_heartbeat_at       TIMESTAMPTZ,
    last_ip                 INET,
    queue_depth             INTEGER DEFAULT 0,
    queue_dropped_count     BIGINT DEFAULT 0,

    -- Cached metadata for display
    hostname                VARCHAR(255),
    os_info                 VARCHAR(255),
    uptime_seconds          BIGINT,

    -- Misc
    tags                    JSONB DEFAULT '[]',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    created_by              UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sensors_status ON sensors(status);
CREATE INDEX IF NOT EXISTS idx_sensors_site   ON sensors(site_id);
CREATE INDEX IF NOT EXISTS idx_sensors_apikey ON sensors(api_key_hash) WHERE api_key_hash IS NOT NULL;

-- ─── Sensor assignments (many-to-many) ───
-- target_type: 'device' | 'service_check' | 'group'
CREATE TABLE IF NOT EXISTS sensor_assignments (
    sensor_id   UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    target_type VARCHAR(20) NOT NULL CHECK (target_type IN ('device','service_check','group')),
    target_id   UUID NOT NULL,
    priority    INTEGER DEFAULT 100,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (sensor_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_sensor_assign_target ON sensor_assignments(target_type, target_id);

-- ─── default_sensor_id columns on existing tables ───
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name='devices' AND column_name='default_sensor_id'
    ) THEN
        ALTER TABLE devices
          ADD COLUMN default_sensor_id UUID REFERENCES sensors(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name='service_checks' AND column_name='default_sensor_id'
    ) THEN
        ALTER TABLE service_checks
          ADD COLUMN default_sensor_id UUID REFERENCES sensors(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ─── updated_at triggers ───
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sites_updated_at') THEN
        CREATE TRIGGER sites_updated_at BEFORE UPDATE ON sites
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sensors_updated_at') THEN
        CREATE TRIGGER sensors_updated_at BEFORE UPDATE ON sensors
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;
