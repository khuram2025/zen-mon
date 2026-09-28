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
UPDATE devices d
   SET serial_number = COALESCE(NULLIF(d.serial_number, ''), c.serial_number),
       firmware_version = COALESCE(NULLIF(d.firmware_version, ''), NULLIF(c.fw_revision, ''))
  FROM (
        SELECT DISTINCT ON (device_id) device_id,
               NULLIF(btrim(serial_number), '') AS serial_number,
               NULLIF(btrim(fw_revision), '')   AS fw_revision
          FROM device_entities
         WHERE class = 'chassis'
         ORDER BY device_id, COALESCE(parent_index, 0), ent_index
       ) c
 WHERE c.device_id = d.id
   AND (c.serial_number IS NOT NULL OR c.fw_revision IS NOT NULL);

COMMIT;
