-- Migration 015: manual maps
-- User-authored map canvases with live device-backed nodes and manual links.

CREATE TABLE IF NOT EXISTS manual_maps (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS manual_map_nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id      UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    label       VARCHAR(255),
    icon        VARCHAR(40) NOT NULL DEFAULT 'auto',
    x_pct       NUMERIC(6,2) NOT NULL DEFAULT 50,
    y_pct       NUMERIC(6,2) NOT NULL DEFAULT 50,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (map_id, device_id)
);

CREATE TABLE IF NOT EXISTS manual_map_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id          UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    source_node_id  UUID NOT NULL REFERENCES manual_map_nodes(id) ON DELETE CASCADE,
    target_node_id  UUID NOT NULL REFERENCES manual_map_nodes(id) ON DELETE CASCADE,
    label           VARCHAR(255),
    link_type       VARCHAR(40) NOT NULL DEFAULT 'manual',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (source_node_id <> target_node_id)
);

CREATE INDEX IF NOT EXISTS idx_manual_maps_created_at
    ON manual_maps(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_manual_map_nodes_map
    ON manual_map_nodes(map_id);
CREATE INDEX IF NOT EXISTS idx_manual_map_nodes_device
    ON manual_map_nodes(device_id);
CREATE INDEX IF NOT EXISTS idx_manual_map_links_map
    ON manual_map_links(map_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_map_links_unique_pair
    ON manual_map_links (
        map_id,
        LEAST(source_node_id, target_node_id),
        GREATEST(source_node_id, target_node_id)
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON manual_maps TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON manual_map_nodes TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON manual_map_links TO zenplus;
    END IF;
END $$;
