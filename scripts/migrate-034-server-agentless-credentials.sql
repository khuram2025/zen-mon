-- Link server inventory rows to saved agentless credential profiles.
-- Optional by mode:
--   agentless_wmi / agentless_winrm -> windows_credential_id
--   snmp -> snmp_credential_id
--   ssh -> ncm_credential_id

ALTER TABLE servers
    ADD COLUMN IF NOT EXISTS windows_credential_id uuid REFERENCES windows_credentials(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS snmp_credential_id uuid REFERENCES snmp_credentials(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS ncm_credential_id uuid REFERENCES ncm_credentials(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_servers_windows_credential ON servers (windows_credential_id);
CREATE INDEX IF NOT EXISTS idx_servers_snmp_credential ON servers (snmp_credential_id);
CREATE INDEX IF NOT EXISTS idx_servers_ncm_credential ON servers (ncm_credential_id);
