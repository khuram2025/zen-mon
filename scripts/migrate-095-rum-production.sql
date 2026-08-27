-- Production browser-RUM control-plane hardening.
--
-- Existing rows remain nullable so the control plane can list and replace
-- legacy keys without a lossy guessed binding.  Intake rejects NULL bindings;
-- every active browser key must be explicitly reissued for one application.

ALTER TABLE apm_ingest_keys
    ADD COLUMN IF NOT EXISTS application_id VARCHAR(128);

CREATE INDEX IF NOT EXISTS idx_apm_ingest_keys_rum_application
    ON apm_ingest_keys (application_id)
    WHERE kind = 'rum' AND revoked_at IS NULL;
