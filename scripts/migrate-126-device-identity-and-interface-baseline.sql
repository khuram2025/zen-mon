-- Migration 126: device identity from ENTITY-MIB + interface "ever up" baseline.
--
-- * devices.firmware_version — the chassis entPhysicalFirmwareRev. The UI's
--   Firmware field had no backing column, so it could never show a value.
--   The poller now copies the chassis serial / model / firmware from
--   device_entities onto the device row after each entity walk.
-- * device_interfaces.last_up_at — last time the poller saw the port
--   operationally up. The "Interface down" rule only alerts on ports that
--   have been up at least once, so unused access ports on a freshly added
--   switch no longer raise one Critical alert each.
--
-- Additive and idempotent.

BEGIN;

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS firmware_version VARCHAR(128);

ALTER TABLE device_interfaces
    ADD COLUMN IF NOT EXISTS last_up_at TIMESTAMPTZ;

-- Baseline: every port currently up has, by definition, been up.
UPDATE device_interfaces
   SET last_up_at = COALESCE(last_seen, NOW())
 WHERE last_up_at IS NULL
   AND lower(COALESCE(oper_status, '')) IN ('up', '1');

-- Seed identity for devices whose entities were already collected.
-- serial_number is unique across devices (managed children key on it): a
-- chassis serial already held elsewhere, or reported by two devices (the
-- same switch added twice), is skipped instead of aborting the upgrade.
WITH chassis AS (
    SELECT DISTINCT ON (device_id) device_id,
           LEFT(NULLIF(btrim(serial_number), ''), 128) AS serial_number,
           LEFT(NULLIF(btrim(fw_revision), ''), 128)   AS fw_revision
      FROM device_entities
     WHERE class = 'chassis'
     ORDER BY device_id, COALESCE(parent_index, 0) <> 0, ent_index
), unique_serial AS (
    SELECT serial_number FROM chassis
     WHERE serial_number IS NOT NULL
     GROUP BY serial_number HAVING count(*) = 1
)
UPDATE devices d
   SET serial_number = CASE
           WHEN NULLIF(d.serial_number, '') IS NULL
            AND c.serial_number IN (SELECT serial_number FROM unique_serial)
            AND NOT EXISTS (SELECT 1 FROM devices o
                             WHERE o.serial_number = c.serial_number AND o.id <> d.id)
           THEN c.serial_number
           ELSE d.serial_number
       END,
       firmware_version = COALESCE(NULLIF(d.firmware_version, ''), c.fw_revision)
  FROM chassis c
 WHERE c.device_id = d.id
   AND (c.serial_number IS NOT NULL OR c.fw_revision IS NOT NULL);

COMMIT;
