-- Migration 019: manual map annotation shapes
--
-- Adds support for standalone shape/text annotations on a manual map. These
-- are NOT bound to any device — they're decorative or structural elements
-- (zone boxes, callouts, labels) used to organize the diagram.
--
-- Rendered beneath device nodes (z_index defaults to 0; nodes/links sit
-- above). Each shape is positioned and sized as percentages of the canvas
-- so the map scales gracefully with the viewport.

CREATE TABLE IF NOT EXISTS manual_map_shapes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id      UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    kind        VARCHAR(20) NOT NULL
                CHECK (kind IN ('rectangle', 'circle', 'text')),
    x_pct       NUMERIC(6,2) NOT NULL DEFAULT 50,
    y_pct       NUMERIC(6,2) NOT NULL DEFAULT 50,
    w_pct       NUMERIC(6,2) NOT NULL DEFAULT 14,
    h_pct       NUMERIC(6,2) NOT NULL DEFAULT 8,
    text        TEXT,
    fill        VARCHAR(40),
    stroke      VARCHAR(40),
    z_index     INTEGER NOT NULL DEFAULT 0,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_manual_map_shapes_map
    ON manual_map_shapes (map_id, z_index);
