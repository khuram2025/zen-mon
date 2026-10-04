-- Explicit profile opt-in. Existing profiles remain strict.
ALTER TABLE ncm_credentials ADD COLUMN IF NOT EXISTS ssh_auto_trust boolean NOT NULL DEFAULT false;
