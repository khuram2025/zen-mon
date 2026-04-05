-- ZenPlus: Service Checks Migration
-- Adds HTTP, TCP, and TLS/SSL monitoring capabilities

CREATE TABLE IF NOT EXISTS service_checks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id       UUID REFERENCES devices(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    check_type      VARCHAR(20) NOT NULL
                    CHECK (check_type IN ('http', 'tcp', 'tls')),
    enabled         BOOLEAN DEFAULT TRUE,
    target_host     VARCHAR(255) NOT NULL,
    target_port     INTEGER,
    target_url      VARCHAR(2048),
    http_method     VARCHAR(10) DEFAULT 'GET'
                    CHECK (http_method IN ('GET', 'POST', 'HEAD', 'PUT')),
    http_headers    JSONB DEFAULT '{}',
    http_body       TEXT,
    http_expected_status INTEGER DEFAULT 200,
    http_content_match VARCHAR(1024),
    http_follow_redirects BOOLEAN DEFAULT TRUE,
    tls_warn_days   INTEGER DEFAULT 30,
    tls_critical_days INTEGER DEFAULT 7,
    check_interval  INTEGER DEFAULT 60,
    timeout         INTEGER DEFAULT 10,
    status          VARCHAR(20) DEFAULT 'unknown'
                    CHECK (status IN ('up', 'down', 'degraded', 'warning', 'unknown')),
    last_check_at   TIMESTAMPTZ,
    last_response_ms DOUBLE PRECISION,
    last_error      TEXT,
    tls_expiry_date TIMESTAMPTZ,
    tls_days_remaining INTEGER,
    tls_issuer      VARCHAR(512),
    tls_subject     VARCHAR(512),
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_service_checks_device ON service_checks(device_id);
CREATE INDEX IF NOT EXISTS idx_service_checks_type ON service_checks(check_type);
CREATE INDEX IF NOT EXISTS idx_service_checks_status ON service_checks(status);
CREATE INDEX IF NOT EXISTS idx_service_checks_enabled ON service_checks(enabled) WHERE enabled = TRUE;

DO $$ BEGIN
    CREATE TRIGGER service_checks_updated_at BEFORE UPDATE ON service_checks
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
