BEGIN;
ALTER TABLE notification_site_peers
    ADD COLUMN credential_expires_at TIMESTAMPTZ,
    ADD COLUMN previous_token_hash TEXT,
    ADD COLUMN previous_valid_until TIMESTAMPTZ,
    ADD COLUMN credential_revoked_at TIMESTAMPTZ;
-- Existing trusts retain their compatibility lifetime until explicitly rotated.
COMMIT;
