BEGIN;
ALTER TABLE host_ingest_capacity ADD COLUMN trap_bytes BIGINT NOT NULL DEFAULT 0 CHECK (trap_bytes>=0);
CREATE TABLE snmp_trap_incident_inbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    source_key TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (octet_length(payload::text)<=131072),
    payload_bytes INTEGER GENERATED ALWAYS AS (octet_length(payload::text)) STORED,
    due_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_type TEXT
);
CREATE INDEX snmp_trap_incident_source ON snmp_trap_incident_inbox(source_key,id);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON snmp_trap_incident_inbox TO zenplus;
        GRANT USAGE,SELECT ON SEQUENCE snmp_trap_incident_inbox_id_seq TO zenplus;
    END IF;
END $$;
COMMIT;
