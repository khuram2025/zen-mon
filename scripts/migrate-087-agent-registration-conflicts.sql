-- Persist tokenless agent registration conflicts so operators can see and
-- resolve them from Agent Fleet instead of diagnosing transient HTTP 409s.
-- The controller stores only SHA-256 hashes of pending secrets; plaintext
-- registration secrets never leave the agent or enter the database.

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS pending_conflict_secret_hash VARCHAR(128),
    ADD COLUMN IF NOT EXISTS registration_conflict_revision UUID,
    ADD COLUMN IF NOT EXISTS registration_conflict_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS registration_conflict_ip INET,
    ADD COLUMN IF NOT EXISTS registration_conflict_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS registration_conflict_install_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS registration_conflict_hostname VARCHAR(255),
    ADD COLUMN IF NOT EXISTS registration_conflict_version VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_agents_registration_conflict
    ON agents(registration_conflict_at DESC)
    WHERE pending_conflict_secret_hash IS NOT NULL;
