-- On-demand network flow capture.
--
-- An operator starts a capture from the server's Network tab; the controller
-- queues a start_network_capture command and the agent streams observed
-- conversations back while it runs. This table is the capture's control
-- record and progress; the flows themselves land in ClickHouse
-- (zenplus.host_network_flows), which is sized for that volume.

CREATE TABLE IF NOT EXISTS network_captures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_id       UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    agent_id        UUID REFERENCES agents(id) ON DELETE SET NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed','expired')),
    interface       VARCHAR(255),
    duration_s      INTEGER NOT NULL DEFAULT 300,
    sample_interval_s INTEGER NOT NULL DEFAULT 2,
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    ends_at         TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    samples         INTEGER NOT NULL DEFAULT 0,
    flow_count      INTEGER NOT NULL DEFAULT 0,
    bytes_sent      BIGINT NOT NULL DEFAULT 0,
    bytes_received  BIGINT NOT NULL DEFAULT 0,
    -- False when the host could not attribute byte counts to flows (needs
    -- TCP ESTATS, i.e. an elevated agent). The UI must not read 0 bytes as
    -- "no traffic" when this is false.
    bytes_available BOOLEAN NOT NULL DEFAULT FALSE,
    truncated       BOOLEAN NOT NULL DEFAULT FALSE,
    note            TEXT,
    error_message   TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_network_captures_server
    ON network_captures (server_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_network_captures_active
    ON network_captures (status) WHERE status IN ('queued','running');

COMMENT ON TABLE network_captures IS
    'Control record for on-demand agent network flow captures. Flows live in ClickHouse.';
