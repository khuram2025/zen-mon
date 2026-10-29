-- Stop legacy notification senders before upgrade or rollback. Old code
-- completes by logical row ID and cannot enforce the attempt token.
BEGIN;

ALTER TABLE alert_delivery_keys
    ADD COLUMN IF NOT EXISTS claim_token UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_delivery_keys_claim_token
    ON alert_delivery_keys (claim_token);

COMMENT ON COLUMN alert_delivery_keys.claim_token IS
    'Rotated on every claim; only the current claimed attempt may record its outcome.';
COMMENT ON TABLE alert_delivery_keys IS
    'Logical delivery identities with attempt ownership; external exactly-once delivery is not guaranteed.';

COMMIT;
