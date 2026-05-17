-- Migration 018: Windows credentials (WMI / WinRM) + link from discovery profiles.
-- Idempotent and safe to re-run.

CREATE TABLE IF NOT EXISTS windows_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(150) NOT NULL UNIQUE,
    username        VARCHAR(150) NOT NULL,
    domain          VARCHAR(150),                              -- optional NetBIOS / DNS domain
    password_enc    BYTEA NOT NULL,                            -- encrypted via app.core.crypto
    auth_method     VARCHAR(20) NOT NULL DEFAULT 'ntlm'
                    CHECK (auth_method IN ('basic','ntlm','kerberos','credssp','certificate')),
    transport       VARCHAR(10) NOT NULL DEFAULT 'http'
                    CHECK (transport IN ('http','https')),
    port            INTEGER NOT NULL DEFAULT 5985,
    ssl_verify      BOOLEAN NOT NULL DEFAULT FALSE,
    description     TEXT,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Linked credentials on a discovery profile (Windows, separate from SNMP).
ALTER TABLE discovery_profiles
    ADD COLUMN IF NOT EXISTS windows_credential_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Same for results: which Windows cred actually worked.
ALTER TABLE discovery_results_v2
    ADD COLUMN IF NOT EXISTS windows_credential_used UUID REFERENCES windows_credentials(id) ON DELETE SET NULL;


DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT ALL ON windows_credentials TO zenplus;
    END IF;
END $$;
