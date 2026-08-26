-- Agent hardware and enriched process inventory.
--
-- Hardware is a slowly-changing host fact, so it belongs on the PostgreSQL
-- server record.  Process state is kept in the last-known inventory tables;
-- absent watchlisted processes use their own name-keyed table because every
-- such sample intentionally carries pid=0.

ALTER TABLE servers
    ADD COLUMN IF NOT EXISTS cpu_model VARCHAR(255),
    ADD COLUMN IF NOT EXISTS cpu_physical_cores INTEGER,
    ADD COLUMN IF NOT EXISTS physical_disks JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN servers.cpu_model IS
    'CPU model reported by the agent hardware inventory.';
COMMENT ON COLUMN servers.cpu_physical_cores IS
    'Physical CPU core count reported by the agent hardware inventory.';
COMMENT ON COLUMN servers.physical_disks IS
    'Bounded physical disk inventory reported by the agent.';

ALTER TABLE server_process_inventory
    ADD COLUMN IF NOT EXISTS state VARCHAR(32) NOT NULL DEFAULT 'running',
    ADD COLUMN IF NOT EXISTS running BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS watchlisted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS server_process_watchlist_inventory (
    server_id UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    cmdline TEXT,
    user_name VARCHAR(255),
    cpu_pct REAL NOT NULL DEFAULT 0,
    memory_bytes BIGINT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    state VARCHAR(32) NOT NULL DEFAULT 'not_running',
    running BOOLEAN NOT NULL DEFAULT FALSE,
    watchlisted BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, name)
);

CREATE INDEX IF NOT EXISTS idx_server_process_watchlist_updated
    ON server_process_watchlist_inventory (server_id, updated_at DESC);

