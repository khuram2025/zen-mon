-- Migration 031: allow the same device to be placed on a map more than once
-- (duplicate-node workflow: copy a styled node, then swap its device profile).

ALTER TABLE manual_map_nodes
    DROP CONSTRAINT IF EXISTS manual_map_nodes_map_id_device_id_key;

-- Keep lookups fast now that the implicit unique index is gone.
CREATE INDEX IF NOT EXISTS idx_manual_map_nodes_map_device
    ON manual_map_nodes(map_id, device_id);
