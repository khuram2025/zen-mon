-- Revocable login sessions and room for authenticated encryption envelopes.
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ALTER COLUMN snmp_community TYPE TEXT;
ALTER TABLE snmp_credentials ALTER COLUMN community TYPE TEXT;
ALTER TABLE snmp_credentials ALTER COLUMN v3_auth_passphrase TYPE TEXT;
ALTER TABLE snmp_credentials ALTER COLUMN v3_priv_passphrase TYPE TEXT;
ALTER TABLE discovery_jobs ALTER COLUMN community TYPE TEXT;
