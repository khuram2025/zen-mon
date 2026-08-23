-- Explicit administrator opt-in for sending service credentials over HTTP.

ALTER TABLE service_checks
    ADD COLUMN IF NOT EXISTS http_allow_insecure_auth BOOLEAN NOT NULL DEFAULT FALSE;
