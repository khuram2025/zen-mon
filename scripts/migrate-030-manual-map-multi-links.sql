-- Migration 030: allow multiple manual links between the same node pair
-- (multi-homed / redundant connectivity on maps).

DROP INDEX IF EXISTS idx_manual_map_links_unique_pair;

CREATE INDEX IF NOT EXISTS idx_manual_map_links_pair
    ON manual_map_links (
        map_id,
        LEAST(source_node_id, target_node_id),
        GREATEST(source_node_id, target_node_id)
    );
