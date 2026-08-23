-- Authenticated, multi-step service monitoring.
-- Secrets use the appliance AES-256-GCM key and are never stored in JSON.

CREATE TABLE IF NOT EXISTS service_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(120) NOT NULL UNIQUE,
    auth_type     VARCHAR(20) NOT NULL
                  CHECK (auth_type IN ('basic', 'bearer', 'form')),
    username      VARCHAR(255),
    secret_cipher BYTEA NOT NULL,
    description   TEXT,
    created_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    CREATE TRIGGER service_credentials_updated_at
        BEFORE UPDATE ON service_credentials
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE service_checks
    ADD COLUMN IF NOT EXISTS credential_id UUID
        REFERENCES service_credentials(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS workflow_operator VARCHAR(8) NOT NULL DEFAULT 'all',
    ADD COLUMN IF NOT EXISTS workflow_steps JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$ BEGIN
    ALTER TABLE service_checks
        ADD CONSTRAINT service_checks_workflow_operator_check
        CHECK (workflow_operator IN ('all', 'any'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_service_checks_credential_id
    ON service_checks(credential_id) WHERE credential_id IS NOT NULL;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON service_credentials TO zenplus;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;
