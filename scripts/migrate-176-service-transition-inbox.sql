BEGIN;
CREATE TABLE service_transition_inbox (
    id BIGSERIAL PRIMARY KEY,
    service_check_id UUID NOT NULL REFERENCES service_checks(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text)<=65536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    processed_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_type TEXT
);
CREATE INDEX service_transition_pending ON service_transition_inbox(service_check_id,id) WHERE processed_at IS NULL;
CREATE INDEX service_transition_processed ON service_transition_inbox(processed_at) WHERE processed_at IS NOT NULL;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON service_transition_inbox TO zenplus;
        GRANT USAGE,SELECT ON SEQUENCE service_transition_inbox_id_seq TO zenplus;
    END IF;
END $$;
COMMIT;
