-- 1.2.x: model server/app/models/device.py declares snmp_credential_id but no migration
-- shipped originally. This adds the column. Idempotent.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS snmp_credential_id UUID;
