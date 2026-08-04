-- NetFlow saved forensic views (see server/app/models/netflow_saved_view.py).
--
-- The model shipped without a migration, so only appliances where the table
-- was created out-of-band have working /api/v1/netflow/saved-views endpoints;
-- fresh installs 500 on them. Idempotent, matches the SQLAlchemy model.

CREATE TABLE IF NOT EXISTS netflow_saved_views (
    id          UUID PRIMARY KEY,
    name        VARCHAR(120) NOT NULL,
    description VARCHAR(500),
    query       JSON NOT NULL,
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    owner_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_netflow_saved_views_updated
    ON netflow_saved_views (pinned DESC, updated_at DESC);

COMMENT ON TABLE netflow_saved_views IS
    'Saved NetFlow forensics queries (name + JSON filter payload) shown on /netflow/saved-views.';
