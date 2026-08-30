-- Migration 101: make browser RUM keys and their SDK settings recallable
--
-- An ingest key was stored as a sha256 hash only, and the RUM snippet settings
-- (service name, release, sampling, consent, privacy, action/long-task capture)
-- were never persisted at all — they lived in the creation dialog's React state
-- and vanished when it closed. Losing the snippet therefore meant revoking a
-- working key and redeploying every page just to read your own configuration
-- back.
--
-- Two columns fix that:
--
--   key_cipher   AES-GCM ciphertext of the plaintext key (app.core.crypto), set
--                for kind='rum' ONLY. A browser RUM key is public by design: it
--                is served inside the HTML of every page and is readable by any
--                visitor, so its confidentiality is not what protects the
--                tenant — the exact origin allowlist and application binding
--                are. Storing it recoverably lets an operator re-read the
--                install snippet without a key rotation.
--
--                Secret collector keys (kind='sdk', zpi_) are deliberately NOT
--                given a cipher. They stay hash-only and unrecoverable, so a
--                database dump still cannot yield a usable collector key.
--
--   rum_options  The snippet settings, so the install tag can be regenerated
--                byte-identically instead of guessed.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE apm_ingest_keys
  ADD COLUMN IF NOT EXISTS key_cipher BYTEA,
  ADD COLUMN IF NOT EXISTS rum_options JSONB;

COMMENT ON COLUMN apm_ingest_keys.key_cipher IS
  'AES-GCM encrypted plaintext key. Populated for kind=''rum'' only (public browser keys); NULL for secret sdk keys, which remain unrecoverable by design.';

COMMENT ON COLUMN apm_ingest_keys.rum_options IS
  'Browser RUM SDK snippet settings (service_name, version, sample_rate_percent, replay_sample_rate_percent, track_actions, track_long_tasks, consent, privacy) so the install snippet can be regenerated.';

COMMIT;
