-- Response-loss retry ledger for host metric uploads. The API records a
-- completed (agent_id, batch_id) outcome before replying, so a retry after a
-- lost HTTP response does not insert the ClickHouse rows again. PostgreSQL and
-- ClickHouse are not one atomic transaction: a process crash after a
-- ClickHouse insert but before this row commits can still replay that insert.

CREATE TABLE IF NOT EXISTS agent_host_result_batches (
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    batch_id VARCHAR(255) NOT NULL,
    server_id UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    payload_sha256 VARCHAR(64) NOT NULL,
    sequence_start BIGINT NOT NULL,
    sequence_end BIGINT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    clock_skew_s INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, batch_id),
    CONSTRAINT ck_agent_host_result_batches_counts
        CHECK (accepted >= 0 AND rejected >= 0)
);

CREATE INDEX IF NOT EXISTS idx_agent_host_result_batches_agent_completed
    ON agent_host_result_batches(agent_id, completed_at);

CREATE INDEX IF NOT EXISTS idx_agent_host_result_batches_server
    ON agent_host_result_batches(server_id);
