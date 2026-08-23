-- Migration 079: tags on interfaces (links)
--
-- Tagging existed for devices and servers but not for the interfaces shown on
-- Link Utilization, which is where operators actually reason about circuits —
-- "every WAN uplink", "the MPLS circuits", "links billed to site B". Those
-- groupings cut across devices, so a device tag cannot express them.
--
-- Assignments live on the interface row as a JSONB text array, matching
-- devices.tags and servers.tags: the same registry (`tags`, migrate-067) owns
-- the catalog, the same jsonb_exists()/@> filters work, and the same GIN index
-- shape serves them. A join table would normalise better but would be the only
-- tagged surface in the product that reads differently.
--
-- Safe against the poller: UpsertInterfaces names its columns explicitly in
-- ON CONFLICT DO UPDATE SET, so an SNMP re-sync never touches this column.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE device_interfaces
  ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN device_interfaces.tags IS
  'Operator tag names for this link, as a JSONB text array. Catalog lives in the tags registry; renames and deletes propagate from /api/v1/tags.';

-- Containment queries (tags @> '["wan"]') are the filter the Link Utilization
-- list runs, and jsonb_path_ops is the smaller, faster index for exactly that.
CREATE INDEX IF NOT EXISTS idx_device_interfaces_tags
  ON device_interfaces USING GIN (tags jsonb_path_ops);

COMMIT;
