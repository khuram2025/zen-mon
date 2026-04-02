-- ZenPlus PostgreSQL Schema

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── Users ───
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(100) NOT NULL UNIQUE,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255),
    role            VARCHAR(20) DEFAULT 'viewer' CHECK (role IN ('admin', 'editor', 'viewer')),
    is_active       BOOLEAN DEFAULT TRUE,
    last_login      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Device Groups ───
CREATE TABLE device_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    color       VARCHAR(7),
    parent_id   UUID REFERENCES device_groups(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Devices ───
CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname        VARCHAR(255) NOT NULL,
    ip_address      INET NOT NULL UNIQUE,
    device_type     VARCHAR(50) NOT NULL DEFAULT 'other'
                    CHECK (device_type IN ('router', 'switch', 'firewall', 'server', 'access_point', 'printer', 'other')),
    location        VARCHAR(255),
    group_id        UUID REFERENCES device_groups(id) ON DELETE SET NULL,
    tags            JSONB DEFAULT '[]',

    -- Monitoring config
    ping_enabled    BOOLEAN DEFAULT TRUE,
    ping_interval   INTEGER DEFAULT 60,
    snmp_enabled    BOOLEAN DEFAULT FALSE,
    snmp_community  VARCHAR(255),
    snmp_version    VARCHAR(5) DEFAULT '2c',
    snmp_port       INTEGER DEFAULT 161,

    -- Current state
    status          VARCHAR(20) DEFAULT 'unknown'
                    CHECK (status IN ('up', 'down', 'degraded', 'unknown', 'maintenance')),
    last_seen       TIMESTAMPTZ,
    last_rtt_ms     DOUBLE PRECISION,

    -- Metadata
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX idx_devices_status ON devices(status);
CREATE INDEX idx_devices_group ON devices(group_id);
CREATE INDEX idx_devices_tags ON devices USING GIN(tags);
CREATE INDEX idx_devices_ip ON devices(ip_address);
CREATE INDEX idx_devices_ping_enabled ON devices(ping_enabled) WHERE ping_enabled = TRUE;

-- ─── Alert Rules ───
CREATE TABLE alert_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    enabled         BOOLEAN DEFAULT TRUE,

    -- Condition
    metric          VARCHAR(50) NOT NULL CHECK (metric IN ('ping_status', 'rtt', 'packet_loss', 'jitter')),
    operator        VARCHAR(10) NOT NULL CHECK (operator IN ('eq', 'neq', 'gt', 'lt', 'gte', 'lte')),
    threshold       DOUBLE PRECISION NOT NULL,
    duration        INTEGER DEFAULT 0,

    -- Scope
    device_id       UUID REFERENCES devices(id) ON DELETE CASCADE,
    group_id        UUID REFERENCES device_groups(id) ON DELETE CASCADE,

    -- Severity
    severity        VARCHAR(20) DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),

    -- Notification
    notify_channels JSONB DEFAULT '[]',
    cooldown        INTEGER DEFAULT 300,

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

-- ─── Alerts ───
CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID REFERENCES alert_rules(id) ON DELETE SET NULL,
    device_id       UUID REFERENCES devices(id) ON DELETE CASCADE,

    status          VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'acknowledged', 'resolved')),
    severity        VARCHAR(20) NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message         TEXT NOT NULL,

    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_at     TIMESTAMPTZ,

    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_alerts_status ON alerts(status);
CREATE INDEX idx_alerts_device ON alerts(device_id);
CREATE INDEX idx_alerts_triggered ON alerts(triggered_at DESC);
CREATE INDEX idx_alerts_active ON alerts(status) WHERE status = 'active';

-- ─── Notification Channels ───
CREATE TABLE notification_channels (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    type        VARCHAR(50) NOT NULL CHECK (type IN ('email', 'sms', 'webhook', 'slack', 'telegram')),
    config      JSONB NOT NULL,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Dashboard Configs ───
CREATE TABLE dashboard_configs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(255) NOT NULL,
    is_default  BOOLEAN DEFAULT FALSE,
    layout      JSONB NOT NULL DEFAULT '[]',
    widgets     JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Updated At Trigger ───
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER devices_updated_at BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER alert_rules_updated_at BEFORE UPDATE ON alert_rules
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER dashboard_configs_updated_at BEFORE UPDATE ON dashboard_configs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─── Seed default admin user (password: admin123) ───
INSERT INTO users (username, email, password_hash, full_name, role)
VALUES ('admin', 'admin@zenplus.local',
        '$2b$12$vjHI8XBgL.dCyn.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai',
        'System Administrator', 'admin');

-- ─── Seed device groups ───
INSERT INTO device_groups (name, description, color) VALUES
    ('Core Network', 'Core routers and switches', '#6366F1'),
    ('Distribution', 'Distribution layer switches', '#22C55E'),
    ('Access Layer', 'Access switches and APs', '#F59E0B'),
    ('Servers', 'Server infrastructure', '#EF4444'),
    ('DMZ', 'DMZ / perimeter devices', '#8B5CF6');
