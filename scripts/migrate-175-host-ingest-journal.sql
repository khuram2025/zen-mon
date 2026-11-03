BEGIN;
CREATE TABLE host_ingest_batches (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    write_token TEXT NOT NULL UNIQUE,
    payload BYTEA,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','committed','held')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    attempted_at TIMESTAMPTZ,
    committed_at TIMESTAMPTZ,
    hold_reason TEXT,
    CHECK ((state='committed') = (payload IS NULL))
);
CREATE INDEX host_ingest_pending ON host_ingest_batches(table_name,id) WHERE state<>'committed';
CREATE TABLE host_ingest_receipts (
    table_name TEXT NOT NULL,
    token_hash BYTEA NOT NULL CHECK (octet_length(token_hash)=32),
    batch_id BIGINT NOT NULL REFERENCES host_ingest_batches(id),
    PRIMARY KEY (table_name,token_hash)
);
CREATE INDEX host_ingest_receipts_batch ON host_ingest_receipts(batch_id);
CREATE TABLE host_ingest_capacity (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    payload_bytes BIGINT NOT NULL DEFAULT 0 CHECK(payload_bytes>=0),
    receipts BIGINT NOT NULL DEFAULT 0 CHECK(receipts>=0)
);
INSERT INTO host_ingest_capacity(singleton) VALUES(TRUE);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT,INSERT,UPDATE ON host_ingest_batches,host_ingest_receipts,host_ingest_capacity TO zenplus;
        GRANT USAGE,SELECT ON SEQUENCE host_ingest_batches_id_seq TO zenplus;
    END IF;
END $$;
COMMIT;
