-- Migration 203: managed parent/child dependencies (alerting audit 2026-11-19,
-- gap G-13; docs/dependencies.md).
--
-- topology_dependencies (migrate-014) suppresses a child's alerts while its
-- parent is down, but rows were only ever written by a discovery rule or the
-- controller sync and could only be parents and children that are devices.
--
-- * child_server_id: a dependency may now point from a device to a server
--   ("this server sits behind that switch"). Exactly one of child_device_id /
--   child_server_id is set; child_device_id becomes nullable for that.
-- * source: who owns the row. 'manual' (added on the Dependencies page),
--   'topology' (a suggestion from the LLDP/CDP topology that an operator
--   accepted; owned like a manual row), 'discovery_rule' and 'controller'
--   (written by those engines, which remain their only writers: the API
--   never deletes them). Existing rows are backfilled from what wrote them:
--   the controller sync's 'controller' type / note, a discovery rule's
--   assignment or note; anything else is 'manual'.
-- * created_by: the user who added a manual or topology row.
--
-- Idempotent: every statement is guarded; a re-run changes nothing.

BEGIN;

ALTER TABLE topology_dependencies
    ADD COLUMN IF NOT EXISTS child_server_id UUID REFERENCES servers(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS source VARCHAR(20),
    ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE topology_dependencies ALTER COLUMN child_device_id DROP NOT NULL;

UPDATE topology_dependencies td
   SET source = CASE
        WHEN td.dependency_type = 'controller' OR td.notes LIKE 'auto: managed-device sync%' THEN 'controller'
        WHEN td.notes LIKE 'Discovery rule:%' OR EXISTS (
                SELECT 1 FROM discovery_engine_assignments a
                 WHERE a.asset_kind = 'device' AND a.asset_id = td.child_device_id
                   AND a.action_key = 'dependency_parent'
                   AND a.item_key = td.parent_device_id::text) THEN 'discovery_rule'
        ELSE 'manual' END
 WHERE td.source IS NULL;

ALTER TABLE topology_dependencies ALTER COLUMN source SET DEFAULT 'manual';
ALTER TABLE topology_dependencies ALTER COLUMN source SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'topology_dependencies_source_check') THEN
        ALTER TABLE topology_dependencies ADD CONSTRAINT topology_dependencies_source_check
            CHECK (source IN ('manual', 'topology', 'discovery_rule', 'controller'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'topology_dependencies_one_child') THEN
        ALTER TABLE topology_dependencies ADD CONSTRAINT topology_dependencies_one_child
            CHECK (num_nonnulls(child_device_id, child_server_id) = 1);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_topology_dependencies_server_unique
    ON topology_dependencies (parent_device_id, child_server_id, dependency_type)
    WHERE child_server_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_topology_dependencies_child_server
    ON topology_dependencies (child_server_id) WHERE child_server_id IS NOT NULL;

COMMENT ON COLUMN topology_dependencies.source IS
    'Owner: manual / topology (operator-managed), discovery_rule / controller (engine-managed; the API never deletes them).';

COMMIT;
