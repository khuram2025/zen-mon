-- Discovery profiles: SSH / CLI connection profiles (ncm_credentials) for authenticated probes.

ALTER TABLE discovery_profiles
    ADD COLUMN IF NOT EXISTS ssh_credential_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
