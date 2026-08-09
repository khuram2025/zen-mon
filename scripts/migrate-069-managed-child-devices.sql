-- migrate-069: Managed child devices (controller-managed APs / switches)
--
-- Promotes rows a controller reports through its monitoring template
-- (FortiGate's managed FortiAPs/FortiSwitches, Aruba's per-AP table) to
-- first-class child devices, so alert rules, maintenance windows, tags and
-- reports apply to each AP/switch — the way SolarWinds/Zabbix model thin APs
-- under a WLC.
--
--  * devices.managed_by_device_id — parent controller (SET NULL on delete:
--    orphaned children keep their history and can be cleaned up manually).
--  * devices.poll_mode — 'direct' (pinged/SNMP-polled as before) or
--    'via_controller' (state derived from the parent's template tables; the
--    poller never touches these directly and they may have no IP at all,
--    hence ip_address becomes nullable).
--  * devices.managed_source / managed_instance — which template group and
--    row on the parent this child was materialized from (the sync upsert
--    identity when no serial is known).
--  * devices.promote_managed — per-controller opt-in. Off by default so an
--    OTA update never silently inflates a fleet's device inventory.
--  * device_profiles.oid_groups[*].children — declarative mapping (added to
--    the Fortinet + Aruba builtins below) telling the sync service that rows
--    of a table group are devices: their type, status column and enum→status
--    translation. Any vendor pack can opt in without code changes.
--  * topology_dependencies gains a 'controller' dependency type: the sync
--    service records parent→child edges so the existing dependency-aware
--    suppression collapses a controller outage into one root-cause alert
--    instead of an alert per AP.

-- ─── devices: child-device columns ───

ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_by_device_id UUID REFERENCES devices(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS poll_mode VARCHAR(20) NOT NULL DEFAULT 'direct';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_number VARCHAR(128);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_ip INET;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_source VARCHAR(64);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_instance VARCHAR(160);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS managed_last_seen TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS promote_managed BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_poll_mode_check;
ALTER TABLE devices ADD CONSTRAINT devices_poll_mode_check
    CHECK (poll_mode IN ('direct', 'via_controller'));

-- Controller-reported children may have no (routable) IP; the UNIQUE
-- constraint stays and simply doesn't apply to NULLs.
ALTER TABLE devices ALTER COLUMN ip_address DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_devices_managed_by
    ON devices(managed_by_device_id) WHERE managed_by_device_id IS NOT NULL;

-- Serial is the durable identity of controller-reported hardware: it lets a
-- child follow its physical box when it re-homes to another controller and
-- blocks a second record for a chassis that is also monitored directly.
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_serial_unique
    ON devices(serial_number) WHERE serial_number IS NOT NULL;

-- Fallback identity when the vendor table exposes no serial: one child per
-- (controller, template group, table row).
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_managed_identity
    ON devices(managed_by_device_id, managed_source, managed_instance)
    WHERE managed_instance IS NOT NULL;

-- ─── topology dependencies: controller edge type ───

ALTER TABLE topology_dependencies DROP CONSTRAINT IF EXISTS topology_dependencies_dependency_type_check;
ALTER TABLE topology_dependencies ADD CONSTRAINT topology_dependencies_dependency_type_check
    CHECK (dependency_type IN ('uplink', 'wan', 'power', 'site', 'service', 'manual', 'controller'));

-- ─── builtin packs: declare which table groups are child devices ───
--
-- children mapping shape (mirrored in server/app/schemas/snmp.py):
--   device_type  devices.device_type for materialized children
--   vendor       devices.vendor
--   status_key   metric key whose enum code drives child status
--   status_map   enum code (string) -> up | down | degraded
--                (codes not in the map yield 'unknown')
--   model_key / os_version_key / serial_key / ip_key
--                optional string metric keys enriching the child record
--
-- Guarded so a replay (or the next builtin refresh) doesn't bump versions
-- again once the mapping is present.

UPDATE device_profiles
SET oid_groups = (
        SELECT jsonb_agg(
            CASE g.value->>'key'
                WHEN 'access_points' THEN g.value || jsonb_build_object('children',
                    '{"device_type":"access_point","vendor":"Fortinet",
                      "status_key":"fgt_ap_status",
                      "status_map":{"2":"up","5":"up","1":"down","0":"degraded","3":"degraded","4":"degraded"},
                      "model_key":"fgt_ap_model"}'::jsonb)
                WHEN 'fortiswitch' THEN g.value || jsonb_build_object('children',
                    '{"device_type":"switch","vendor":"Fortinet",
                      "status_key":"fgt_sw_status",
                      "status_map":{"1":"up","0":"down"},
                      "os_version_key":"fgt_sw_version"}'::jsonb)
                ELSE g.value
            END
            ORDER BY g.ordinality)
        FROM jsonb_array_elements(oid_groups) WITH ORDINALITY AS g(value, ordinality)
    ),
    version = version + 1,
    updated_at = NOW()
WHERE builtin = TRUE
  AND name = 'Fortinet FortiGate'
  AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(oid_groups) AS g(value)
        WHERE g.value->>'key' IN ('access_points', 'fortiswitch')
          AND NOT g.value ? 'children'
  );

UPDATE device_profiles
SET oid_groups = (
        SELECT jsonb_agg(
            CASE g.value->>'key'
                WHEN 'access_points' THEN g.value || jsonb_build_object('children',
                    '{"device_type":"access_point","vendor":"Aruba",
                      "status_key":"aruba_ap_status",
                      "status_map":{"1":"up","2":"down"},
                      "model_key":"aruba_ap_model","ip_key":"aruba_ap_ip"}'::jsonb)
                ELSE g.value
            END
            ORDER BY g.ordinality)
        FROM jsonb_array_elements(oid_groups) WITH ORDINALITY AS g(value, ordinality)
    ),
    version = version + 1,
    updated_at = NOW()
WHERE builtin = TRUE
  AND name = 'Aruba Wireless Controller'
  AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(oid_groups) AS g(value)
        WHERE g.value->>'key' = 'access_points'
          AND NOT g.value ? 'children'
  );
