-- migrate-189-device-templates.sql
-- Device Templates, phase 1 (docs/plans/device-templates-plan.md, docs/device-templates.md).
--
-- 1. device_types: the device type catalogue. Replaces the seven-value CHECK
--    on devices.device_type with a foreign key, so new types (load balancer,
--    wireless controller, UPS, storage, ...) are data, not a schema change.
--    Every value the old CHECK allowed is seeded, so no existing row changes.
-- 2. device_profiles (the templates) gain an identity and a history:
--      builtin_key        stable key of a built-in template (survives renames)
--      parent_id/overlay  a customised copy of another template: only the
--                         differences are stored, the parent keeps receiving
--                         updates and oid_groups holds the resolved result
--      device_type, icon  what a device on this template is
--      match_priority     higher wins when several templates match a device
--      revision           bumped on every published change
--      draft              unpublished edits (devices keep the published copy)
--      pilot_*            a draft published to a few devices only
--      content_hash       md5 of the published match_rules + oid_groups; a
--                         row changed outside the API (a release migration
--                         updating a built-in) is noticed and recorded
--    oid_groups keeps its meaning for every reader (poller, device page,
--    alert evaluator): the template's effective, published groups.
-- 3. device_profile_versions: one row per published revision (snapshot).
-- 4. devices: who assigned the template and when, and a suggestion when a
--    different template now matches (never applied automatically).
--
-- Idempotent: tolerates a re-run. Existing templates, devices and readings
-- are unchanged; nothing is re-classified.

-- ── 1. Device type catalogue ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_types (
    key         VARCHAR(50) PRIMARY KEY,
    label       VARCHAR(80) NOT NULL,
    category    VARCHAR(20) NOT NULL DEFAULT 'other',
    icon        VARCHAR(40) NOT NULL DEFAULT 'other',
    sort_order  INTEGER NOT NULL DEFAULT 100,
    builtin     BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT device_types_key_format CHECK (key ~ '^[a-z][a-z0-9_]{0,49}$'),
    CONSTRAINT device_types_category CHECK (category IN (
        'network', 'security', 'wireless', 'server', 'power', 'storage', 'endpoint', 'other')),
    CONSTRAINT device_types_label_len CHECK (length(btrim(label)) BETWEEN 1 AND 80)
);

INSERT INTO device_types (key, label, category, icon, sort_order, builtin) VALUES
    ('router',              'Router',              'network',  'router',              10, TRUE),
    ('switch',              'Switch',              'network',  'switch',              20, TRUE),
    ('firewall',            'Firewall',            'security', 'firewall',            30, TRUE),
    ('load_balancer',       'Load Balancer',       'network',  'load_balancer',       40, TRUE),
    ('sdwan_edge',          'SD-WAN Edge',         'network',  'router',              45, TRUE),
    ('wireless_controller', 'Wireless Controller', 'wireless', 'wireless_controller', 50, TRUE),
    ('access_point',        'Access Point',        'wireless', 'access_point',        60, TRUE),
    ('server',              'Server',              'server',   'server',              70, TRUE),
    ('storage',             'Storage',             'storage',  'storage',             80, TRUE),
    ('ups',                 'UPS',                 'power',    'ups',                 90, TRUE),
    ('pdu',                 'PDU',                 'power',    'ups',                 95, TRUE),
    ('printer',             'Printer',             'endpoint', 'printer',            100, TRUE),
    ('ip_camera',           'IP Camera',           'endpoint', 'camera',             110, TRUE),
    ('voip',                'VoIP Phone',          'endpoint', 'phone',              120, TRUE),
    ('other',               'Other',               'other',    'other',              999, TRUE)
ON CONFLICT (key) DO NOTHING;

-- Any value already stored that the catalogue does not know (only possible on
-- a database whose CHECK was removed by hand) becomes a custom type, so the
-- foreign key below never rejects existing data.
INSERT INTO device_types (key, label, category, icon, sort_order, builtin)
SELECT DISTINCT d.device_type, initcap(replace(d.device_type, '_', ' ')), 'other', 'other', 500, FALSE
  FROM devices d
 WHERE d.device_type IS NOT NULL
   AND d.device_type ~ '^[a-z][a-z0-9_]{0,49}$'
   AND NOT EXISTS (SELECT 1 FROM device_types t WHERE t.key = d.device_type)
ON CONFLICT (key) DO NOTHING;
UPDATE devices SET device_type = 'other'
 WHERE device_type IS NULL
    OR NOT EXISTS (SELECT 1 FROM device_types t WHERE t.key = devices.device_type);

DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'devices'::regclass AND contype = 'c'
           AND pg_get_constraintdef(oid) ILIKE '%device_type%'
    LOOP
        EXECUTE format('ALTER TABLE devices DROP CONSTRAINT %I', c.conname);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'devices_device_type_fkey') THEN
        ALTER TABLE devices ADD CONSTRAINT devices_device_type_fkey
            FOREIGN KEY (device_type) REFERENCES device_types(key) ON UPDATE CASCADE;
    END IF;
END $$;

COMMENT ON TABLE device_types IS
  'Device type catalogue (devices.device_type references it). category is the roll-up shown in charts; icon is a dashboard network-icon key.';

-- ── 2. Template identity, overlays, drafts, pilots ─────────────────────────
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS builtin_key VARCHAR(64);
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES device_profiles(id) ON DELETE RESTRICT;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS overlay JSONB;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS parent_hash TEXT;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS device_type VARCHAR(50)
    REFERENCES device_types(key) ON UPDATE CASCADE ON DELETE SET NULL;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS icon VARCHAR(40);
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS match_priority INTEGER NOT NULL DEFAULT 100;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS draft JSONB;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS draft_updated_at TIMESTAMPTZ;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS draft_updated_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS pilot_device_ids UUID[];
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS pilot_oid_groups JSONB;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS pilot_started_at TIMESTAMPTZ;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES users(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_profiles_match_priority_range') THEN
        ALTER TABLE device_profiles ADD CONSTRAINT device_profiles_match_priority_range
            CHECK (match_priority BETWEEN 0 AND 1000);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_profiles_overlay_has_parent') THEN
        ALTER TABLE device_profiles ADD CONSTRAINT device_profiles_overlay_has_parent
            CHECK ((parent_id IS NULL) = (overlay IS NULL) AND parent_id IS DISTINCT FROM id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_profiles_pilot_pair') THEN
        ALTER TABLE device_profiles ADD CONSTRAINT device_profiles_pilot_pair
            CHECK ((pilot_device_ids IS NULL) = (pilot_oid_groups IS NULL));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS device_profiles_builtin_key
    ON device_profiles (builtin_key) WHERE builtin_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS device_profiles_parent ON device_profiles (parent_id) WHERE parent_id IS NOT NULL;

-- Names stay unique per (name, version) in the database, as before: rows that
-- share a name with another version may exist. The API keeps new and renamed
-- templates unique by name (under the templates advisory lock).

-- Built-in identity and default device types.
--
-- Several built-in rows can share a name (one per version): a seed migration
-- re-applied after a later migration bumped that row's version (063 then 069
-- for 'Aruba Wireless Controller', likewise the other seeds) inserts the old
-- version again. Every such row gets the default device type, but only ONE row
-- per name gets the canonical key: the one most devices use, then the highest
-- version, then the most recently updated. The others get a derived key below
-- and stay what they were (built-in templates, devices unchanged).
--
-- Revised 2026-11-17: the first cut keyed every row with the name and failed
-- on the unique index (device_profiles_builtin_key) on such appliances, after
-- everything above had run. Rerunning this file from that state converges.
-- Where the first cut succeeded (one row per name) this revision leaves the
-- same result, so run-migrations reconciles its checksum instead of re-running.
CREATE TEMP TABLE zp189_builtin (n TEXT, k TEXT, t TEXT);
INSERT INTO zp189_builtin (n, k, t) VALUES
    ('Fortinet FortiGate',               'fortinet_fortigate',   'firewall'),
    ('Cisco IOS/IOS-XE Switch & Router', 'cisco_ios',            NULL),
    ('Cisco ASA Firewall',               'cisco_asa',            'firewall'),
    ('Palo Alto PAN-OS Firewall',        'paloalto_panos',       'firewall'),
    ('F5 BIG-IP',                        'f5_bigip',             'load_balancer'),
    ('Juniper JunOS',                    'juniper_junos',        NULL),
    ('Aruba Wireless Controller',        'aruba_wlc',            'wireless_controller'),
    ('Aruba AOS-CX Switch',              'aruba_aoscx',          'switch'),
    ('Dell SmartFabric OS10 Switch',     'dell_os10',            'switch'),
    ('MikroTik RouterOS',                'mikrotik_routeros',    'router'),
    ('Linux Server',                     'linux_server',         'server'),
    ('Windows Server',                   'windows_server',       'server');

UPDATE device_profiles p
   SET device_type = v.t
  FROM zp189_builtin v
 WHERE p.name = v.n AND p.builtin AND p.device_type IS NULL AND v.t IS NOT NULL;

UPDATE device_profiles p
   SET builtin_key = pick.k
  FROM (SELECT DISTINCT ON (v.k) c.id, v.k
          FROM zp189_builtin v
          JOIN device_profiles c ON c.name = v.n AND c.builtin AND c.builtin_key IS NULL
         WHERE NOT EXISTS (SELECT 1 FROM device_profiles o WHERE o.builtin_key = v.k)
         ORDER BY v.k,
                  (SELECT count(*) FROM devices d WHERE d.profile_id = c.id) DESC,
                  c.version DESC, c.updated_at DESC, c.id) AS pick
 WHERE p.id = pick.id;

DROP TABLE zp189_builtin;

UPDATE device_profiles
   SET builtin_key = left(regexp_replace(lower(name), '[^a-z0-9]+', '_', 'g'), 48) || '_' || left(md5(id::text), 8)
 WHERE builtin AND builtin_key IS NULL;

UPDATE device_profiles
   SET content_hash = md5(COALESCE(match_rules, '{}'::jsonb)::text || '|' || COALESCE(oid_groups, '[]'::jsonb)::text)
 WHERE content_hash IS NULL;

COMMENT ON COLUMN device_profiles.oid_groups IS
  'Effective, published metric groups (for an overlay: the parent''s groups with the overlay applied). Read by the poller and the device page.';
COMMENT ON COLUMN device_profiles.overlay IS
  'Differences from parent_id (disabled groups/metrics, interval/threshold changes, added groups). NULL for a standalone template.';
COMMENT ON COLUMN device_profiles.pilot_oid_groups IS
  'Groups the devices in pilot_device_ids poll instead of oid_groups while a pilot publish is running.';

-- ── 3. Published revisions ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_profile_versions (
    id              BIGSERIAL PRIMARY KEY,
    profile_id      UUID NOT NULL REFERENCES device_profiles(id) ON DELETE CASCADE,
    revision        INTEGER NOT NULL,
    action          VARCHAR(20) NOT NULL,
    snapshot        JSONB NOT NULL,
    reason          TEXT,
    changed_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    changed_by_name TEXT,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT device_profile_versions_rev UNIQUE (profile_id, revision),
    CONSTRAINT device_profile_versions_action CHECK (action IN (
        'baseline', 'created', 'published', 'rollback', 'imported', 'cloned',
        'customized', 'external_update', 'parent_update')),
    CONSTRAINT device_profile_versions_reason_len CHECK (reason IS NULL OR length(reason) <= 500)
);

INSERT INTO device_profile_versions (profile_id, revision, action, snapshot, reason)
SELECT p.id, p.revision, 'baseline',
       jsonb_build_object('name', p.name, 'vendor', p.vendor, 'description', p.description,
                          'match_rules', p.match_rules, 'oid_groups', p.oid_groups,
                          'device_type', p.device_type, 'icon', p.icon,
                          'match_priority', p.match_priority, 'overlay', p.overlay),
       'State before Device Templates history began'
  FROM device_profiles p
ON CONFLICT (profile_id, revision) DO NOTHING;

-- ── 4. Assignment source and suggestions on devices ────────────────────────
ALTER TABLE devices ADD COLUMN IF NOT EXISTS profile_source VARCHAR(10);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS profile_assigned_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS profile_suggested_id UUID REFERENCES device_profiles(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS profile_suggested_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS device_type_source VARCHAR(10);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'devices_profile_source_values') THEN
        ALTER TABLE devices ADD CONSTRAINT devices_profile_source_values
            CHECK (profile_source IS NULL OR profile_source IN ('auto', 'rule', 'manual', 'import'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'devices_device_type_source_values') THEN
        ALTER TABLE devices ADD CONSTRAINT devices_device_type_source_values
            CHECK (device_type_source IS NULL OR device_type_source IN ('auto', 'template', 'rule', 'manual'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS devices_profile_suggested ON devices (profile_suggested_id)
    WHERE profile_suggested_id IS NOT NULL;

COMMENT ON COLUMN devices.profile_source IS
  'Who set profile_id: auto (classifier), rule (discovery rule), manual (operator), import. NULL = set before 1.25.10 (not recorded).';
COMMENT ON COLUMN devices.profile_suggested_id IS
  'A different template the classifier now matches for this device. Shown for review; never applied automatically.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_types TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_profile_versions TO zenplus;
        GRANT USAGE, SELECT ON SEQUENCE device_profile_versions_id_seq TO zenplus;
    END IF;
END $$;
