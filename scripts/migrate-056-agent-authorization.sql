-- Agent authorization workflow.
--
-- Tokenless agents register as pending and prove continuity with a local,
-- OS-protected pending secret. An operator authorizes the row; the next
-- matching poll receives a newly minted API key whose plaintext is never
-- stored by the controller. Revocation invalidates that key immediately.

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS pending_secret_hash VARCHAR(128),
    ADD COLUMN IF NOT EXISTS authorized_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS authorized_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS authorization_source VARCHAR(32),
    ADD COLUMN IF NOT EXISTS enrollment_token_prefix VARCHAR(32);

UPDATE agents
SET authorized_at = COALESCE(api_key_rotated_at, created_at),
    authorization_source = COALESCE(authorization_source, 'legacy')
WHERE api_key_hash IS NOT NULL
  AND authorized_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agents_pending_authorization
    ON agents(status, created_at)
    WHERE authorized_at IS NULL AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agents_authorization_state
    ON agents(authorized_at, revoked_at);
