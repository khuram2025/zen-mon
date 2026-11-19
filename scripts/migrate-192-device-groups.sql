-- migrate-192-device-groups.sql
-- Device Templates phase 2: groups you can use (docs/device-groups.md).
--
-- 1. device_groups become a managed tree with two kinds:
--      static   members are chosen: a device's primary group (devices.group_id)
--               plus devices or servers added to it explicitly
--      dynamic  members are computed from a condition over device and server
--               facts (the discovery-rule condition language), re-evaluated by
--               a background pass when facts or the condition change
-- 2. device_group_members holds every membership with its source
--    ('primary', 'static', 'rule'). A trigger keeps the 'primary' rows equal to
--    devices.group_id, so everything that scoped by group before covers the same
--    devices after the upgrade.
-- 3. device_group_member_closure (view) rolls members up to every ancestor
--    group: a rule or maintenance window on a parent group covers its subgroups.
-- 4. custom_field_defs / custom_field_values: typed fields on devices and
--    servers (text, number, date, choice, yes/no).
--
-- Idempotent: tolerates a re-run. No existing group, device or rule changes.

-- ── 1. Groups ──────────────────────────────────────────────────────────────
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'static';
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS condition JSONB;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS asset_kinds TEXT[] NOT NULL DEFAULT ARRAY['device']::text[];
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS member_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS evaluated_at TIMESTAMPTZ;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS eval_error TEXT;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE device_groups ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_groups_kind_values') THEN
        ALTER TABLE device_groups ADD CONSTRAINT device_groups_kind_values
            CHECK (kind IN ('static', 'dynamic'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_groups_dynamic_condition') THEN
        ALTER TABLE device_groups ADD CONSTRAINT device_groups_dynamic_condition
            CHECK ((kind = 'dynamic') = (condition IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_groups_not_own_parent') THEN
        ALTER TABLE device_groups ADD CONSTRAINT device_groups_not_own_parent
            CHECK (parent_id IS DISTINCT FROM id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_groups_asset_kinds') THEN
        ALTER TABLE device_groups ADD CONSTRAINT device_groups_asset_kinds
            CHECK (cardinality(asset_kinds) >= 1 AND asset_kinds <@ ARRAY['device', 'server']::text[]);
    END IF;
END $$;

-- ── 2. Membership ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_group_members (
    group_id   UUID NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    asset_kind VARCHAR(10) NOT NULL,
    asset_id   UUID NOT NULL,
    source     VARCHAR(10) NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, asset_kind, asset_id, source),
    CONSTRAINT device_group_members_kind CHECK (asset_kind IN ('device', 'server')),
    CONSTRAINT device_group_members_source CHECK (source IN ('primary', 'static', 'rule'))
);
CREATE INDEX IF NOT EXISTS device_group_members_asset ON device_group_members (asset_kind, asset_id);

INSERT INTO device_group_members (group_id, asset_kind, asset_id, source)
SELECT group_id, 'device', id, 'primary' FROM devices WHERE group_id IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION zp_device_primary_group_member() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.group_id IS NOT DISTINCT FROM OLD.group_id THEN
        RETURN NEW;
    END IF;
    DELETE FROM device_group_members
     WHERE asset_kind = 'device' AND asset_id = NEW.id AND source = 'primary';
    IF NEW.group_id IS NOT NULL THEN
        INSERT INTO device_group_members (group_id, asset_kind, asset_id, source)
        VALUES (NEW.group_id, 'device', NEW.id, 'primary')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS devices_primary_group_member ON devices;
CREATE TRIGGER devices_primary_group_member
    AFTER INSERT OR UPDATE OF group_id ON devices
    FOR EACH ROW EXECUTE FUNCTION zp_device_primary_group_member();

-- Group membership and custom field values of a deleted device or server go
-- with it (asset_id is polymorphic, so no foreign key can do it).
CREATE OR REPLACE FUNCTION zp_asset_deleted_cleanup() RETURNS trigger AS $$
BEGIN
    DELETE FROM device_group_members WHERE asset_kind = TG_ARGV[0] AND asset_id = OLD.id;
    DELETE FROM custom_field_values WHERE asset_kind = TG_ARGV[0] AND asset_id = OLD.id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- ── 4. Custom fields (before the cleanup triggers reference the table) ──────
CREATE TABLE IF NOT EXISTS custom_field_defs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key          VARCHAR(50) NOT NULL UNIQUE,
    label        VARCHAR(80) NOT NULL,
    field_type   VARCHAR(10) NOT NULL,
    choices      JSONB,
    asset_kinds  TEXT[] NOT NULL DEFAULT ARRAY['device', 'server']::text[],
    description  TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 100,
    show_in_list BOOLEAN NOT NULL DEFAULT FALSE,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT custom_field_defs_key_format CHECK (key ~ '^[a-z][a-z0-9_]{0,49}$'),
    CONSTRAINT custom_field_defs_type CHECK (field_type IN ('text', 'number', 'date', 'choice', 'boolean')),
    CONSTRAINT custom_field_defs_choices CHECK ((field_type = 'choice') = (choices IS NOT NULL)),
    CONSTRAINT custom_field_defs_asset_kinds CHECK (
        cardinality(asset_kinds) >= 1 AND asset_kinds <@ ARRAY['device', 'server']::text[])
);

CREATE TABLE IF NOT EXISTS custom_field_values (
    field_id   UUID NOT NULL REFERENCES custom_field_defs(id) ON DELETE CASCADE,
    asset_kind VARCHAR(10) NOT NULL,
    asset_id   UUID NOT NULL,
    value_text TEXT,
    value_num  DOUBLE PRECISION,
    value_date DATE,
    value_bool BOOLEAN,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (field_id, asset_kind, asset_id),
    CONSTRAINT custom_field_values_kind CHECK (asset_kind IN ('device', 'server')),
    CONSTRAINT custom_field_values_text_len CHECK (value_text IS NULL OR length(value_text) <= 1000)
);
CREATE INDEX IF NOT EXISTS custom_field_values_asset ON custom_field_values (asset_kind, asset_id);

DROP TRIGGER IF EXISTS devices_asset_cleanup ON devices;
CREATE TRIGGER devices_asset_cleanup
    AFTER DELETE ON devices FOR EACH ROW EXECUTE FUNCTION zp_asset_deleted_cleanup('device');
DROP TRIGGER IF EXISTS servers_asset_cleanup ON servers;
CREATE TRIGGER servers_asset_cleanup
    AFTER DELETE ON servers FOR EACH ROW EXECUTE FUNCTION zp_asset_deleted_cleanup('server');

-- ── 3. Closure over the group tree ─────────────────────────────────────────
-- Every group with itself and its ancestors (depth-limited, so a cycle written
-- outside the API cannot loop).
CREATE OR REPLACE VIEW device_group_ancestry AS
WITH RECURSIVE a(group_id, ancestor_id, depth) AS (
    SELECT id, id, 0 FROM device_groups
    UNION ALL
    SELECT a.group_id, g.parent_id, a.depth + 1
      FROM a JOIN device_groups g ON g.id = a.ancestor_id
     WHERE g.parent_id IS NOT NULL AND a.depth < 8
)
SELECT DISTINCT group_id, ancestor_id FROM a;

-- Members of a group including the members of its subgroups.
CREATE OR REPLACE VIEW device_group_member_closure AS
SELECT DISTINCT ga.ancestor_id AS group_id, m.asset_kind, m.asset_id
  FROM device_group_members m
  JOIN device_group_ancestry ga ON ga.group_id = m.group_id;

UPDATE device_groups g
   SET member_count = (SELECT count(*) FROM device_group_member_closure c WHERE c.group_id = g.id);

COMMENT ON TABLE device_group_members IS
  'Group membership: primary (devices.group_id, kept by trigger), static (added explicitly) or rule (dynamic group condition).';
COMMENT ON VIEW device_group_member_closure IS
  'Members of each group including members of its subgroups. Alert rule and maintenance scopes use this.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_group_members, custom_field_defs, custom_field_values TO zenplus;
        GRANT SELECT ON device_group_ancestry, device_group_member_closure TO zenplus;
    END IF;
END $$;
