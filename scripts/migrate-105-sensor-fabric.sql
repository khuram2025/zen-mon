-- Migration 105: production remote-sensor correctness and lifecycle state.
--
-- This is deliberately additive.  It introduces one authoritative device
-- ownership resolver, durable ingest acknowledgements, sensor lifecycle
-- events, and per-vantage service state without rewriting migration 008.

BEGIN;

ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS heartbeat_interval_s INTEGER NOT NULL DEFAULT 30,
    ADD COLUMN IF NOT EXISTS degraded_after_s INTEGER NOT NULL DEFAULT 90,
    ADD COLUMN IF NOT EXISTS offline_after_s INTEGER NOT NULL DEFAULT 180,
    ADD COLUMN IF NOT EXISTS status_reason TEXT,
    ADD COLUMN IF NOT EXISTS min_supported_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS bootstrap_config JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sensors_health_intervals_check') THEN
        ALTER TABLE sensors ADD CONSTRAINT sensors_health_intervals_check CHECK (
            heartbeat_interval_s > 0
            AND degraded_after_s >= heartbeat_interval_s
            AND offline_after_s > degraded_after_s
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS sensor_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind VARCHAR(64) NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sensor_events_sensor_ts
    ON sensor_events(sensor_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_events_ts
    ON sensor_events(ts DESC);

CREATE TABLE IF NOT EXISTS sensor_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    verb VARCHAR(32) NOT NULL
        CHECK (verb IN ('update', 'flush_buffer', 'reload_config', 'set_log_level')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'succeeded', 'failed', 'expired')),
    delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
    last_delivered_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
    result TEXT,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sensor_commands_sensor_created
    ON sensor_commands(sensor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_commands_pending
    ON sensor_commands(sensor_id, created_at)
    WHERE status IN ('pending', 'delivered');
CREATE UNIQUE INDEX IF NOT EXISTS uq_sensor_commands_active_verb
    ON sensor_commands(sensor_id, verb)
    WHERE status IN ('pending', 'delivered');

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS sensor_id UUID REFERENCES sensors(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_sensor
    ON alerts(sensor_id, triggered_at DESC)
    WHERE sensor_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_open_sensor_dedupe
    ON alerts(sensor_id, (metadata->>'dedupe'))
    WHERE sensor_id IS NOT NULL
      AND status IN ('pending', 'active', 'acknowledged')
      AND metadata->>'dedupe' IS NOT NULL;

CREATE TABLE IF NOT EXISTS sensor_ingest_ledger (
    sensor_id UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    endpoint VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sensor_id, endpoint, idempotency_key),
    CONSTRAINT sensor_ingest_ledger_counts_check CHECK (accepted >= 0 AND dropped >= 0)
);

CREATE INDEX IF NOT EXISTS idx_sensor_ingest_ledger_created
    ON sensor_ingest_ledger(created_at);

CREATE TABLE IF NOT EXISTS sensor_transition_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id UUID NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    endpoint VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    transition_type VARCHAR(16) NOT NULL
        CHECK (transition_type IN ('device', 'service')),
    entity_id UUID NOT NULL,
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sensor_id, endpoint, idempotency_key, transition_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_sensor_transition_outbox_pending
    ON sensor_transition_outbox(next_attempt_at, created_at)
    WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_sensor_transition_outbox_entity_fifo
    ON sensor_transition_outbox(transition_type, entity_id, created_at, id)
    WHERE processed_at IS NULL;

ALTER TABLE service_checks
    ADD COLUMN IF NOT EXISTS consensus_mode VARCHAR(16) NOT NULL DEFAULT 'any',
    ADD COLUMN IF NOT EXISTS consensus_k INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'service_checks_consensus_mode_check') THEN
        ALTER TABLE service_checks ADD CONSTRAINT service_checks_consensus_mode_check
            CHECK (consensus_mode IN ('any', 'majority', 'threshold'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'service_checks_consensus_k_check') THEN
        ALTER TABLE service_checks ADD CONSTRAINT service_checks_consensus_k_check
            CHECK (consensus_k IS NULL OR consensus_k >= 1);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS service_check_vantage_status (
    service_check_id UUID NOT NULL REFERENCES service_checks(id) ON DELETE CASCADE,
    poller_id TEXT NOT NULL,
    state VARCHAR(16) NOT NULL CHECK (state IN ('up', 'down', 'warning', 'unknown')),
    last_change_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_result_at TIMESTAMPTZ NOT NULL,
    last_latency_ms DOUBLE PRECISION,
    last_error TEXT,
    last_tls_expiry_date TIMESTAMPTZ,
    last_tls_days_remaining INTEGER,
    last_tls_valid BOOLEAN,
    last_tls_issuer VARCHAR(512),
    last_tls_subject VARCHAR(512),
    last_content_matched BOOLEAN,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_check_id, poller_id)
);

CREATE INDEX IF NOT EXISTS idx_service_check_vantage_status_poller
    ON service_check_vantage_status(poller_id, updated_at DESC);

-- One deterministic owner for every device.  A disabled sensor releases its
-- ownership back to the central poller; an offline/degraded sensor retains it
-- so a WAN failure cannot silently change the measurement vantage.
CREATE OR REPLACE VIEW device_polling_owner AS
WITH candidates AS (
    SELECT a.target_id AS device_id, a.sensor_id, 1 AS source_rank,
           COALESCE(a.priority, 100) AS priority, 'explicit'::TEXT AS source
      FROM sensor_assignments a
     WHERE a.target_type = 'device'
    UNION ALL
    SELECT d.id AS device_id, a.sensor_id, 2 AS source_rank,
           COALESCE(a.priority, 100) AS priority, 'group'::TEXT AS source
      FROM devices d
      JOIN sensor_assignments a
        ON a.target_type = 'group' AND a.target_id = d.group_id
    UNION ALL
    SELECT d.id AS device_id, d.default_sensor_id AS sensor_id, 3 AS source_rank,
           100 AS priority, 'default'::TEXT AS source
      FROM devices d
     WHERE d.default_sensor_id IS NOT NULL
), ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER (
               PARTITION BY c.device_id
               ORDER BY c.source_rank, c.priority, c.sensor_id::TEXT
           ) AS rn
      FROM candidates c
      JOIN sensors s ON s.id = c.sensor_id
                    AND s.status IN ('online', 'degraded', 'offline')
)
SELECT d.id AS device_id,
       CASE WHEN r.sensor_id IS NULL THEN 'central' ELSE 'sensor' END::TEXT AS owner_kind,
       r.sensor_id,
       COALESCE(r.source, 'unassigned')::TEXT AS source,
       r.priority
  FROM devices d
  LEFT JOIN ranked r ON r.device_id = d.id AND r.rn = 1;

COMMENT ON VIEW device_polling_owner IS
    'Authoritative device polling owner: explicit sensor assignment, then group, then default; disabled sensors release ownership to central.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON sensor_events TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON sensor_commands TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON sensor_ingest_ledger TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON sensor_transition_outbox TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON service_check_vantage_status TO zenplus;
        GRANT SELECT ON device_polling_owner TO zenplus;
    END IF;
END $$;

COMMIT;
