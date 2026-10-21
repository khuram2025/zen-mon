-- Migration 167: network map entity nodes and map versions.
--
--  * manual_map_nodes can represent a monitored server or a service check as
--    well as a device. Exactly one of device_id / server_id / service_check_id
--    is set (CHECK num_nonnulls(...) = 1). Deleting the server or check removes
--    its map nodes, the same way deleting a device always has. Existing rows
--    are all device nodes, so the constraint validates without a backfill.
--  * manual_map_versions keeps point-in-time snapshots of a map (nodes, links,
--    shapes and map metadata including annotation links) so an operator can
--    restore after a bulk change. The API keeps the newest 50 per map.
--
-- Additive and idempotent; one transaction so the grant cannot be left behind.

BEGIN;

ALTER TABLE manual_map_nodes ALTER COLUMN device_id DROP NOT NULL;

ALTER TABLE manual_map_nodes
    ADD COLUMN IF NOT EXISTS server_id UUID REFERENCES servers(id) ON DELETE CASCADE;
ALTER TABLE manual_map_nodes
    ADD COLUMN IF NOT EXISTS service_check_id UUID REFERENCES service_checks(id) ON DELETE CASCADE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'manual_map_nodes_one_entity_check'
           AND conrelid = 'manual_map_nodes'::regclass
    ) THEN
        ALTER TABLE manual_map_nodes
            ADD CONSTRAINT manual_map_nodes_one_entity_check
            CHECK (num_nonnulls(device_id, server_id, service_check_id) = 1) NOT VALID;
    END IF;
END $$;

ALTER TABLE manual_map_nodes VALIDATE CONSTRAINT manual_map_nodes_one_entity_check;

CREATE INDEX IF NOT EXISTS idx_manual_map_nodes_server
    ON manual_map_nodes(server_id) WHERE server_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_manual_map_nodes_service_check
    ON manual_map_nodes(service_check_id) WHERE service_check_id IS NOT NULL;

COMMENT ON COLUMN manual_map_nodes.server_id IS
    'Monitored server this node represents (exactly one of device_id, server_id, service_check_id).';
COMMENT ON COLUMN manual_map_nodes.service_check_id IS
    'Service check this node represents (exactly one of device_id, server_id, service_check_id).';

CREATE TABLE IF NOT EXISTS manual_map_versions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id      UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    reason      VARCHAR(120),
    node_count  INTEGER NOT NULL DEFAULT 0,
    link_count  INTEGER NOT NULL DEFAULT 0,
    shape_count INTEGER NOT NULL DEFAULT 0,
    snapshot    JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_map_versions_map_created
    ON manual_map_versions (map_id, created_at DESC);

COMMENT ON TABLE manual_map_versions IS
    'Point-in-time snapshots of a manual map (name, description, metadata, nodes, links, shapes).';

-- Large inline images (data: URLs) in snapshots are stored once per map and
-- referenced by hash, so 50 snapshots of a map with uploaded images do not
-- hold 50 copies of each image.
CREATE TABLE IF NOT EXISTS manual_map_version_blobs (
    map_id      UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    sha256      CHAR(64) NOT NULL,
    data        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (map_id, sha256)
);

COMMENT ON TABLE manual_map_version_blobs IS
    'Deduplicated inline images referenced from manual_map_versions snapshots (zp-version-blob:<sha256>).';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON manual_map_versions TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON manual_map_version_blobs TO zenplus;
    END IF;
END $$;

COMMIT;
