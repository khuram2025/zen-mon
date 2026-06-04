-- Migration 020: manual map styling — expanded shape kinds + map metadata
--
-- Adds:
--   * manual_maps.metadata JSONB — holds background image URL/data-URI,
--     theme preset, snap settings, and other per-map UI state that the
--     previous schema had no place to store.
--   * Relaxes manual_map_shapes.kind to allow line, arrow, diamond,
--     hexagon, image, and sticky shapes so the frontend can draw richer
--     annotation primitives (callouts, connectors, region badges).
--
-- All new state is additive — existing rows keep working unchanged.

ALTER TABLE manual_maps
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';

ALTER TABLE manual_map_shapes
    DROP CONSTRAINT IF EXISTS manual_map_shapes_kind_check;

ALTER TABLE manual_map_shapes
    ADD CONSTRAINT manual_map_shapes_kind_check
    CHECK (kind IN (
        'rectangle', 'circle', 'text',
        'line', 'arrow', 'diamond', 'hexagon', 'image', 'sticky'
    ));
