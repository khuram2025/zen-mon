-- migrate-027-ncm-normhash.sql
-- E4: store a normalized-config hash so backups dedup correctly even when the
-- device re-encrypts secrets / rotates volatile fields on every show
-- (e.g. FortiOS conf_file_ver + ENC blobs). Dedup + change-detection use norm_hash.
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS norm_hash TEXT;
