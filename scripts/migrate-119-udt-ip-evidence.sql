-- Preserve independent reporter/interface observations without fabricating
-- repeat evidence from legacy history. Existing observation periods remain.
BEGIN;
CREATE TABLE IF NOT EXISTS udt_ip_evidence (
    endpoint_id UUID NOT NULL REFERENCES udt_endpoints(id) ON DELETE CASCADE,
    ip INET NOT NULL,
    reporting_device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_index INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    observation_count BIGINT NOT NULL DEFAULT 1 CHECK (observation_count > 0),
    recent_sightings TIMESTAMPTZ[] NOT NULL,
    PRIMARY KEY (endpoint_id, ip, reporting_device_id, if_index, source)
);
CREATE INDEX IF NOT EXISTS idx_udt_ip_evidence_ip ON udt_ip_evidence(ip, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_udt_ip_history_endpoint_periods
    ON udt_ip_history (endpoint_id, ip, first_seen DESC, id DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON udt_ip_evidence TO zenplus;
COMMIT;
