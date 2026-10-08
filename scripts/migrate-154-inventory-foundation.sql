-- Migration 154: Inventory & Compliance foundations (phase 0, dark ship).
--
-- Adds the raw-evidence columns, the normalised-identity table that later
-- phases fill in, and database-side change capture, so install / upgrade /
-- firmware / hardware history starts accumulating before any UI exists.
-- History cannot be backfilled, so this ships first. Design, sections 3.2,
-- 3.3, 5 and 6: Documentation/Inventory/02-ARCHITECTURE-DATA-MODEL-API.md.
--
--  * inventory_identity        one normalised identity row per asset, marked
--                              dirty (norm_rev NULL) by triggers and filled in
--                              by the application's normalise loop. values_rev
--                              records which rule-pack revision produced the
--                              stored values and is never reset (see below).
--  * inventory_change_events   append-only history, written by triggers so it
--                              does not matter which writer (Go poller, agent
--                              ingest, API) made the change. No foreign keys:
--                              history outlives the asset.
--  * inventory_daily_snapshots trend counts, written by the daily loop.
--
-- Why every trigger below has a column list and a WHEN clause: devices is
-- written on every poll and server_software_inventory on every agent upload,
-- with the same values nearly every time. UPDATE OF <columns> keeps unrelated
-- updates from considering the trigger at all, and WHEN is evaluated by the
-- executor, so a row whose identity did not change never enters PL/pgSQL.
-- device_template_values is written for dozens of metrics per poll; its WHEN
-- clauses admit only the four identity metric keys. That list is hard-coded in
-- SQL on purpose (the WHEN clause must stay a constant), so a rule pack that
-- starts reading another template-backed metric needs a forward migration that
-- widens it; test_inventory_migration_154 ties the list to the normaliser.
--
-- Suppression: bulk operations that must not read as real changes (rule-pack
-- re-normalisation, key-widening migrations, the seed at the bottom of this
-- file) run with  SET LOCAL zenplus.inventory_suppress = 'on'.  That silences
-- every capture trigger. Dirty-marking is deliberately NOT suppressed: a
-- re-normalise still has to be able to mark rows dirty.
--
-- Rule-pack revisions never read as changes. norm_rev says "this row is
-- current" and dirty-marking resets it to NULL, which would forget which pack
-- rendered the stored values. values_rev is that memory: every writer of
-- normalised identity values (the normalise loop and agent ingest) sets
-- values_rev together with norm_rev, dirty-marking never touches it, and
-- inventory_identity_changed fires only when the old and new values_rev are
-- the same. A pack that renders '10.0.20348.2700' as '10.0.20348' therefore
-- writes no history, whichever writer made the update and whether or not the
-- row was dirty. The price is that a genuine change landing in the very write
-- that moves the revision is absorbed; a false event is worse than a missing
-- one because history cannot be corrected afterwards.
--
-- Version gaps never read as changes either. A collector that momentarily
-- reports no version is a gap, not an upgrade: identity rows and software rows
-- record 'changed' only when both the old and the new value are present.
--
-- Truncated snapshots: when servers.software_truncated is set (the agent sent
-- a capped prefix of the list), every software event written while it is set
-- carries detail.snapshot_truncated = true, because what the list adds or
-- drops at the cap boundary is an artefact of the cap and a reader can then
-- discount it. The flag is read when the event is written, so the ingest must
-- stamp it before it writes the rows.
--
-- servers.inventory_collected_at is the agent-side time of the newest
-- inventory snapshot the ingest accepted; the ingest uses it to ignore a stale
-- or out-of-order snapshot instead of letting it rewind the table and write
-- history in both directions.
--
-- Nothing here emits events for pre-existing data. The seed baselines servers
-- that already hold software, so only differences from this point on count.
--
-- Idempotent and safe to re-run, including on a database that already ran an
-- earlier cut of this file (the columns and index are added if missing and the
-- triggers are replaced).

BEGIN;

-- ------------------------------------------------------- evidence columns

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS sys_descr           text,
    ADD COLUMN IF NOT EXISTS identity_updated_at timestamptz;

ALTER TABLE servers
    ADD COLUMN IF NOT EXISTS inventory_updated_at   timestamptz,
    ADD COLUMN IF NOT EXISTS inventory_collected_at timestamptz,
    ADD COLUMN IF NOT EXISTS inventory_baselined_at timestamptz,
    ADD COLUMN IF NOT EXISTS hw_manufacturer        varchar(128),
    ADD COLUMN IF NOT EXISTS hw_model               varchar(255),
    ADD COLUMN IF NOT EXISTS hw_serial              varchar(128),
    ADD COLUMN IF NOT EXISTS bios_version           varchar(128),
    ADD COLUMN IF NOT EXISTS bios_date              date,
    ADD COLUMN IF NOT EXISTS virtualization         varchar(32),
    ADD COLUMN IF NOT EXISTS software_truncated     boolean NOT NULL DEFAULT false;

ALTER TABLE server_software_inventory
    ADD COLUMN IF NOT EXISTS architecture     varchar(16),
    ADD COLUMN IF NOT EXISTS source           varchar(32),
    ADD COLUMN IF NOT EXISTS install_location text,
    ADD COLUMN IF NOT EXISTS first_seen_at    timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS name_key         varchar(160),
    ADD COLUMN IF NOT EXISTS publisher_key    varchar(96),
    ADD COLUMN IF NOT EXISTS category         varchar(32),
    ADD COLUMN IF NOT EXISTS norm_rev         integer;

CREATE INDEX IF NOT EXISTS idx_ssi_name_key  ON server_software_inventory (name_key, version);
CREATE INDEX IF NOT EXISTS idx_ssi_publisher ON server_software_inventory (publisher_key);
-- Unkeyed rows (norm_rev NULL) answer "which rows still need a key" without
-- walking the table. Right after this migration every existing row is unkeyed,
-- so the index starts out as large as the table; ingest stamps norm_rev on
-- every row it writes and the backfill does the rest, so it shrinks to a few
-- kilobytes (vacuum reclaims the entries of rows that got a key).
CREATE INDEX IF NOT EXISTS idx_ssi_unkeyed
    ON server_software_inventory (server_id, package_name) WHERE norm_rev IS NULL;

-- ------------------------------------------------------------------ tables

CREATE TABLE IF NOT EXISTS inventory_identity (
    asset_kind        varchar(16)  NOT NULL
                      CHECK (asset_kind IN ('server', 'device', 'endpoint')),
    asset_id          uuid         NOT NULL,
    vendor_key        varchar(64),
    vendor_label      varchar(128),
    os_family         varchar(32),
    os_product        varchar(128),
    os_release        varchar(64),
    os_version        varchar(128),
    os_build          varchar(64),
    os_edition        varchar(64),
    os_source         varchar(16),
    hw_vendor         varchar(128),
    hw_model          varchar(255),
    hw_serial         varchar(128),
    hw_source         varchar(16),
    lifecycle_slug    varchar(64),
    -- Rule-pack revision that produced this row; NULL means dirty.
    norm_rev          integer,
    -- Rule-pack revision that produced the stored vendor_*, os_* and hw_*
    -- values. Set by every writer of those values together with norm_rev;
    -- dirty-marking leaves it alone. NULL until the first normalisation.
    values_rev        integer,
    source_changed_at timestamptz  NOT NULL DEFAULT now(),
    normalized_at     timestamptz,
    PRIMARY KEY (asset_kind, asset_id)
);

-- A database that ran an earlier cut of this file already has the table.
ALTER TABLE inventory_identity
    ADD COLUMN IF NOT EXISTS values_rev integer;

CREATE INDEX IF NOT EXISTS idx_inv_identity_os    ON inventory_identity (os_family, os_release, os_version);
CREATE INDEX IF NOT EXISTS idx_inv_identity_hw    ON inventory_identity (vendor_key, hw_model);
CREATE INDEX IF NOT EXISTS idx_inv_identity_dirty ON inventory_identity (asset_kind) WHERE norm_rev IS NULL;

CREATE TABLE IF NOT EXISTS inventory_change_events (
    id          bigserial    PRIMARY KEY,
    observed_at timestamptz  NOT NULL DEFAULT now(),
    asset_kind  varchar(16)  NOT NULL,
    -- No foreign key: history survives deletion of the asset.
    asset_id    uuid         NOT NULL,
    asset_name  varchar(255),
    category    varchar(16)  NOT NULL
                CHECK (category IN ('software', 'os', 'firmware', 'hardware')),
    action      varchar(16)  NOT NULL
                CHECK (action IN ('added', 'removed', 'changed')),
    subject     varchar(255) NOT NULL,
    subject_key varchar(160),
    old_value   text,
    new_value   text,
    detail      jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_inv_events_time  ON inventory_change_events (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_asset ON inventory_change_events (asset_kind, asset_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_cat   ON inventory_change_events (category, action, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_events_key   ON inventory_change_events (subject_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS inventory_daily_snapshots (
    day       date         NOT NULL,
    metric    varchar(64)  NOT NULL,
    dimension varchar(255) NOT NULL DEFAULT '',
    value     bigint       NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);

-- --------------------------------------------- dirty-marking (not suppressed)

-- AFTER INSERT / AFTER UPDATE on devices and servers. TG_ARGV[0] is the asset
-- kind. Seeds the identity row on insert; on update resets it to dirty.
CREATE OR REPLACE FUNCTION inventory_identity_mark_dirty() RETURNS trigger AS $$
BEGIN
    INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev, source_changed_at)
    VALUES (TG_ARGV[0], NEW.id, NULL, now())
    ON CONFLICT (asset_kind, asset_id)
    DO UPDATE SET norm_rev = NULL, source_changed_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- device_template_values rows belong to a device through device_id, not id.
CREATE OR REPLACE FUNCTION inventory_identity_mark_dirty_from_template() RETURNS trigger AS $$
BEGIN
    INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev, source_changed_at)
    VALUES ('device', NEW.device_id, NULL, now())
    ON CONFLICT (asset_kind, asset_id)
    DO UPDATE SET norm_rev = NULL, source_changed_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- AFTER DELETE on devices and servers. Change events are kept on purpose.
CREATE OR REPLACE FUNCTION inventory_identity_remove() RETURNS trigger AS $$
BEGIN
    DELETE FROM inventory_identity
     WHERE asset_kind = TG_ARGV[0] AND asset_id = OLD.id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_devices_seed_ins ON devices;
CREATE TRIGGER inventory_devices_seed_ins
    AFTER INSERT ON devices
    FOR EACH ROW
    EXECUTE FUNCTION inventory_identity_mark_dirty('device');

DROP TRIGGER IF EXISTS inventory_devices_dirty_upd ON devices;
CREATE TRIGGER inventory_devices_dirty_upd
    AFTER UPDATE OF vendor, model, os_version, firmware_version, serial_number,
                    sys_object_id, sys_descr
    ON devices
    FOR EACH ROW
    WHEN (ROW(OLD.vendor, OLD.model, OLD.os_version, OLD.firmware_version,
              OLD.serial_number, OLD.sys_object_id, OLD.sys_descr)
          IS DISTINCT FROM
          ROW(NEW.vendor, NEW.model, NEW.os_version, NEW.firmware_version,
              NEW.serial_number, NEW.sys_object_id, NEW.sys_descr))
    EXECUTE FUNCTION inventory_identity_mark_dirty('device');

DROP TRIGGER IF EXISTS inventory_devices_identity_del ON devices;
CREATE TRIGGER inventory_devices_identity_del
    AFTER DELETE ON devices
    FOR EACH ROW
    EXECUTE FUNCTION inventory_identity_remove('device');

DROP TRIGGER IF EXISTS inventory_servers_seed_ins ON servers;
CREATE TRIGGER inventory_servers_seed_ins
    AFTER INSERT ON servers
    FOR EACH ROW
    EXECUTE FUNCTION inventory_identity_mark_dirty('server');

DROP TRIGGER IF EXISTS inventory_servers_dirty_upd ON servers;
CREATE TRIGGER inventory_servers_dirty_upd
    AFTER UPDATE OF os_name, os_version, kernel_or_build, architecture,
                    cpu_model, hw_manufacturer, hw_model, hw_serial
    ON servers
    FOR EACH ROW
    WHEN (ROW(OLD.os_name, OLD.os_version, OLD.kernel_or_build, OLD.architecture,
              OLD.cpu_model, OLD.hw_manufacturer, OLD.hw_model, OLD.hw_serial)
          IS DISTINCT FROM
          ROW(NEW.os_name, NEW.os_version, NEW.kernel_or_build, NEW.architecture,
              NEW.cpu_model, NEW.hw_manufacturer, NEW.hw_model, NEW.hw_serial))
    EXECUTE FUNCTION inventory_identity_mark_dirty('server');

DROP TRIGGER IF EXISTS inventory_servers_identity_del ON servers;
CREATE TRIGGER inventory_servers_identity_del
    AFTER DELETE ON servers
    FOR EACH ROW
    EXECUTE FUNCTION inventory_identity_remove('server');

-- Template values that carry an exact network OS version or serial: FortiGate
-- firmware, PAN-OS, TMOS and the F5 chassis serial. (fgt_sw_version is a
-- managed-child table whose value lands in the child device's os_version, so
-- the devices trigger above already covers it.) An INSERT trigger's WHEN may
-- not reference OLD, hence one trigger per operation.
DROP TRIGGER IF EXISTS inventory_dtv_dirty_ins ON device_template_values;
CREATE TRIGGER inventory_dtv_dirty_ins
    AFTER INSERT ON device_template_values
    FOR EACH ROW
    WHEN (NEW.metric_key IN ('fgt_fw_version', 'pan_sw_version', 'f5_version', 'f5_serial'))
    EXECUTE FUNCTION inventory_identity_mark_dirty_from_template();

DROP TRIGGER IF EXISTS inventory_dtv_dirty_upd ON device_template_values;
CREATE TRIGGER inventory_dtv_dirty_upd
    AFTER UPDATE OF value_text ON device_template_values
    FOR EACH ROW
    WHEN (NEW.metric_key IN ('fgt_fw_version', 'pan_sw_version', 'f5_version', 'f5_serial')
          AND OLD.value_text IS DISTINCT FROM NEW.value_text)
    EXECUTE FUNCTION inventory_identity_mark_dirty_from_template();

-- ------------------------------------------------ software change capture

-- One function for the three software triggers. Each statement is a single
-- INSERT ... SELECT from servers, which applies two gates at once: the parent
-- row must exist (so rows removed by a server's ON DELETE CASCADE write
-- nothing: the server is already gone when the cascade runs) and its
-- inventory_baselined_at must be set (the first snapshot of a server is a
-- baseline, not a series of installs). asset_name is looked up at event time,
-- and so is software_truncated: it becomes detail.snapshot_truncated (absent,
-- not false, when the snapshot was complete, because jsonb_strip_nulls drops it).
CREATE OR REPLACE FUNCTION inventory_software_capture() RETURNS trigger AS $$
BEGIN
    IF COALESCE(current_setting('zenplus.inventory_suppress', true), 'off') = 'on' THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, subject_key, old_value, new_value, detail)
        SELECT 'server', s.id, s.display_name, 'software', 'added',
               NEW.package_name, NEW.name_key, NULL, NEW.version,
               jsonb_strip_nulls(jsonb_build_object(
                   'vendor', NEW.vendor,
                   'architecture', NEW.architecture,
                   'source', NEW.source,
                   'snapshot_truncated', CASE WHEN s.software_truncated THEN true END))
          FROM servers s
         WHERE s.id = NEW.server_id AND s.inventory_baselined_at IS NOT NULL;
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, subject_key, old_value, new_value, detail)
        SELECT 'server', s.id, s.display_name, 'software', 'changed',
               NEW.package_name, NEW.name_key, OLD.version, NEW.version,
               jsonb_strip_nulls(jsonb_build_object(
                   'vendor', NEW.vendor,
                   'architecture', NEW.architecture,
                   'source', NEW.source,
                   'snapshot_truncated', CASE WHEN s.software_truncated THEN true END))
          FROM servers s
         WHERE s.id = NEW.server_id AND s.inventory_baselined_at IS NOT NULL;
    ELSE
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, subject_key, old_value, new_value, detail)
        SELECT 'server', s.id, s.display_name, 'software', 'removed',
               OLD.package_name, OLD.name_key, OLD.version, NULL,
               jsonb_strip_nulls(jsonb_build_object(
                   'vendor', OLD.vendor,
                   'architecture', OLD.architecture,
                   'source', OLD.source,
                   'snapshot_truncated', CASE WHEN s.software_truncated THEN true END))
          FROM servers s
         WHERE s.id = OLD.server_id AND s.inventory_baselined_at IS NOT NULL;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_ssi_added ON server_software_inventory;
CREATE TRIGGER inventory_ssi_added
    AFTER INSERT ON server_software_inventory
    FOR EACH ROW
    EXECUTE FUNCTION inventory_software_capture();

DROP TRIGGER IF EXISTS inventory_ssi_changed ON server_software_inventory;
CREATE TRIGGER inventory_ssi_changed
    AFTER UPDATE OF version ON server_software_inventory
    FOR EACH ROW
    WHEN (OLD.version IS NOT NULL AND NEW.version IS NOT NULL
          AND OLD.version <> NEW.version)
    EXECUTE FUNCTION inventory_software_capture();

DROP TRIGGER IF EXISTS inventory_ssi_removed ON server_software_inventory;
CREATE TRIGGER inventory_ssi_removed
    AFTER DELETE ON server_software_inventory
    FOR EACH ROW
    EXECUTE FUNCTION inventory_software_capture();

-- ------------------------------------------------ identity change capture

-- Fires when the normalise loop (or ingest) changes a normalised value that
-- was already known. NULL -> value is discovery and value -> NULL is a gap in
-- a collector (poller writes are sticky); neither is a change, and the WHEN
-- clause keeps them out of PL/pgSQL. A write that moves values_rev is a new
-- rule pack re-rendering the same source data, not a change, whoever made it.
-- A device's OS version is its firmware. subject is the OS/firmware product
-- when it is known.
CREATE OR REPLACE FUNCTION inventory_identity_capture() RETURNS trigger AS $$
DECLARE
    v_name varchar(255);
BEGIN
    IF COALESCE(current_setting('zenplus.inventory_suppress', true), 'off') = 'on' THEN
        RETURN NULL;
    END IF;

    IF NEW.asset_kind = 'device' THEN
        SELECT d.hostname INTO v_name FROM devices d WHERE d.id = NEW.asset_id;
    ELSIF NEW.asset_kind = 'server' THEN
        SELECT s.display_name INTO v_name FROM servers s WHERE s.id = NEW.asset_id;
    END IF;

    IF OLD.os_version IS NOT NULL AND NEW.os_version IS NOT NULL
       AND OLD.os_version <> NEW.os_version THEN
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, old_value, new_value, detail)
        VALUES (NEW.asset_kind, NEW.asset_id, v_name,
                CASE WHEN NEW.asset_kind = 'device' THEN 'firmware' ELSE 'os' END,
                'changed', COALESCE(NEW.os_product, 'os_version'),
                OLD.os_version, NEW.os_version,
                jsonb_strip_nulls(jsonb_build_object(
                    'os_family', NEW.os_family,
                    'os_release', NEW.os_release,
                    'os_source', NEW.os_source)));
    END IF;

    IF OLD.hw_serial IS NOT NULL AND NEW.hw_serial IS NOT NULL
       AND OLD.hw_serial <> NEW.hw_serial THEN
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, old_value, new_value, detail)
        VALUES (NEW.asset_kind, NEW.asset_id, v_name, 'hardware', 'changed',
                'hw_serial', OLD.hw_serial, NEW.hw_serial,
                jsonb_strip_nulls(jsonb_build_object(
                    'hw_vendor', NEW.hw_vendor,
                    'hw_model', NEW.hw_model,
                    'hw_source', NEW.hw_source)));
    END IF;

    IF OLD.hw_model IS NOT NULL AND NEW.hw_model IS NOT NULL
       AND OLD.hw_model <> NEW.hw_model THEN
        INSERT INTO inventory_change_events
            (asset_kind, asset_id, asset_name, category, action,
             subject, old_value, new_value, detail)
        VALUES (NEW.asset_kind, NEW.asset_id, v_name, 'hardware', 'changed',
                'hw_model', OLD.hw_model, NEW.hw_model,
                jsonb_strip_nulls(jsonb_build_object(
                    'hw_vendor', NEW.hw_vendor,
                    'hw_source', NEW.hw_source)));
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_identity_changed ON inventory_identity;
CREATE TRIGGER inventory_identity_changed
    AFTER UPDATE OF os_version, hw_serial, hw_model ON inventory_identity
    FOR EACH ROW
    WHEN (OLD.values_rev IS NOT DISTINCT FROM NEW.values_rev
          AND ((OLD.os_version IS NOT NULL AND NEW.os_version IS NOT NULL
                AND OLD.os_version <> NEW.os_version)
               OR (OLD.hw_serial IS NOT NULL AND NEW.hw_serial IS NOT NULL
                   AND OLD.hw_serial <> NEW.hw_serial)
               OR (OLD.hw_model IS NOT NULL AND NEW.hw_model IS NOT NULL
                   AND OLD.hw_model <> NEW.hw_model)))
    EXECUTE FUNCTION inventory_identity_capture();

-- ------------------------------------------------------------------ grants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE
        ON inventory_identity, inventory_change_events, inventory_daily_snapshots
        TO zenplus;
    GRANT USAGE, SELECT ON SEQUENCE inventory_change_events_id_seq TO zenplus;
  END IF;
END $$;

-- -------------------------------------------------------------------- seed
--
-- Every existing asset gets a dirty identity row so the normalise loop picks
-- it up. Servers that already hold software are already baselined: their next
-- upload diffs normally. Servers with no software stay unbaselined, so their
-- first snapshot is silent. Suppression is on so nothing here can read as a
-- change; it ends with the transaction.
SET LOCAL zenplus.inventory_suppress = 'on';

INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev, source_changed_at)
SELECT 'server', s.id, NULL, now() FROM servers s
ON CONFLICT (asset_kind, asset_id) DO NOTHING;

INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev, source_changed_at)
SELECT 'device', d.id, NULL, now() FROM devices d
ON CONFLICT (asset_kind, asset_id) DO NOTHING;

UPDATE servers s
   SET inventory_baselined_at = now()
 WHERE s.inventory_baselined_at IS NULL
   AND EXISTS (SELECT 1 FROM server_software_inventory i WHERE i.server_id = s.id);

COMMIT;
