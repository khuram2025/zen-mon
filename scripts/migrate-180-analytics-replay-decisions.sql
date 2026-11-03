BEGIN;
CREATE TABLE host_ingest_replay_decisions (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES host_ingest_batches(id),
    decision TEXT NOT NULL CHECK (decision IN ('confirm_committed','retry_proven_absent')),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    attempted_at TIMESTAMPTZ NOT NULL,
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    evidence TEXT NOT NULL CHECK (length(evidence) BETWEEN 1 AND 1024),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX host_ingest_replay_batch ON host_ingest_replay_decisions(batch_id,id);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT,INSERT ON host_ingest_replay_decisions TO zenplus;
        GRANT USAGE,SELECT ON SEQUENCE host_ingest_replay_decisions_id_seq TO zenplus;
    END IF;
END $$;
COMMIT;
