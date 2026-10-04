ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS ssh_legacy_compat boolean NOT NULL DEFAULT false;
DROP INDEX IF EXISTS ncm_host_keys_active;
CREATE UNIQUE INDEX ncm_host_keys_active ON ncm_host_keys(host,port,key_type) WHERE retired_at IS NULL;
