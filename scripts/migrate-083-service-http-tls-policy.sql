-- Per-check TLS verification policy for HTTP(S) service checks.
-- Verification remains enabled by default; operators may explicitly disable it
-- for internal services using self-signed or otherwise untrusted certificates.

ALTER TABLE service_checks
    ADD COLUMN IF NOT EXISTS http_ignore_tls_errors BOOLEAN NOT NULL DEFAULT FALSE;
