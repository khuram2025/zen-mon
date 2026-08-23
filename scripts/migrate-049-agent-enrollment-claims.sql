-- Idempotent, per-host enrollment-token claims.
--
-- Fixed-use rollout tokens count distinct agent_uid values, not HTTP attempts.
-- The API locks the parent token row before reading/inserting this table, so
-- concurrent first enrollments serialize and cannot exceed max_uses. A retry
-- for the same primary key remains valid even after the token reaches capacity.

CREATE TABLE IF NOT EXISTS agent_enrollment_claims (
    token_id          UUID NOT NULL
                      REFERENCES agent_enrollment_tokens(id) ON DELETE CASCADE,
    agent_uid         VARCHAR(128) NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    first_claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_ip          INET,
    last_ip           INET,
    PRIMARY KEY (token_id, agent_uid)
);

CREATE INDEX IF NOT EXISTS idx_agent_enrollment_claims_last
    ON agent_enrollment_claims (last_claimed_at DESC);

COMMENT ON TABLE agent_enrollment_claims IS
    'One unique host claim per enrollment token; retries increment attempts without consuming another use.';
