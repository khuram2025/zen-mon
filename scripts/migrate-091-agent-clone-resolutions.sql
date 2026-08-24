-- Durable, administrator-reviewed outcomes for cloned agents that report an
-- existing agent_uid with a different protected pending-registration secret.
-- Plaintext secrets are never stored: the mapping is bound to the SHA-256
-- continuity-secret hash already supplied by the candidate installation.

CREATE TABLE IF NOT EXISTS agent_registration_resolutions (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reported_agent_uid         VARCHAR(128) NOT NULL,
    pending_secret_hash        VARCHAR(128) NOT NULL,
    action                     VARCHAR(32) NOT NULL
                               CHECK (action IN ('register_clone', 'block')),
    source_agent_id            UUID REFERENCES agents(id) ON DELETE SET NULL,
    assigned_agent_id          UUID REFERENCES agents(id) ON DELETE CASCADE,
    assigned_agent_uid         VARCHAR(128),
    conflict_revision          UUID NOT NULL,
    candidate_ip               INET,
    candidate_hostname         VARCHAR(255),
    candidate_version          VARCHAR(32),
    candidate_install_id       VARCHAR(128),
    approved_by                UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_claimed_at           TIMESTAMPTZ,
    last_seen_at               TIMESTAMPTZ,
    retry_count                INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_agent_registration_resolution_candidate
        UNIQUE (reported_agent_uid, pending_secret_hash),
    CONSTRAINT ck_agent_registration_resolution_assignment
        CHECK (
            (action = 'register_clone' AND assigned_agent_id IS NOT NULL
             AND assigned_agent_uid IS NOT NULL)
            OR
            (action = 'block' AND assigned_agent_id IS NULL
             AND assigned_agent_uid IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_agent_registration_resolutions_assigned
    ON agent_registration_resolutions(assigned_agent_id)
    WHERE assigned_agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_registration_resolutions_source
    ON agent_registration_resolutions(source_agent_id, approved_at DESC);

