-- Infrastructure supervisor owns this table; do not grant the application role.
BEGIN;
CREATE TABLE IF NOT EXISTS controller_authority (
    cell_id TEXT PRIMARY KEY,
    node_id TEXT,
    boot_id UUID,
    token UUID,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((node_id IS NULL) = (token IS NULL))
);
CREATE TABLE IF NOT EXISTS controller_authority_events (
    id BIGSERIAL PRIMARY KEY,
    cell_id TEXT NOT NULL,
    generation BIGINT NOT NULL,
    node_id TEXT NOT NULL,
    prior_node_id TEXT,
    fence_receipt JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
COMMIT;
