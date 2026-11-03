BEGIN;
ALTER TABLE sensor_transition_outbox ALTER COLUMN sensor_id DROP NOT NULL;
ALTER TABLE sensor_transition_outbox ADD CONSTRAINT sensor_transition_source CHECK (
    sensor_id IS NOT NULL OR (transition_type='device' AND endpoint IN ('central','managed'))
);
ALTER TABLE sensor_transition_outbox ADD COLUMN queue_order BIGINT;
CREATE SEQUENCE device_transition_queue_order AS BIGINT OWNED BY sensor_transition_outbox.queue_order;
WITH ordered AS (
    SELECT id,row_number() OVER (ORDER BY created_at,id) AS position
    FROM sensor_transition_outbox
)
UPDATE sensor_transition_outbox q SET queue_order=ordered.position FROM ordered WHERE q.id=ordered.id;
SELECT setval('device_transition_queue_order',COALESCE(max(queue_order),0)+1,false) FROM sensor_transition_outbox;
ALTER TABLE sensor_transition_outbox ALTER COLUMN queue_order SET DEFAULT nextval('device_transition_queue_order');
ALTER TABLE sensor_transition_outbox ALTER COLUMN queue_order SET NOT NULL;
CREATE INDEX device_transition_fifo ON sensor_transition_outbox(transition_type,entity_id,queue_order)
    WHERE processed_at IS NULL;
CREATE INDEX device_transition_ready ON sensor_transition_outbox(queue_order) WHERE processed_at IS NULL;

CREATE TABLE device_map_webhook_work (
    id BIGSERIAL PRIMARY KEY,
    delivery_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    map_id UUID NOT NULL REFERENCES manual_maps(id) ON DELETE CASCADE,
    payload JSONB NOT NULL,
    destination_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sending','accepted','failed','uncertain','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    response_status INTEGER
);
CREATE INDEX device_map_webhook_pending ON device_map_webhook_work(id) WHERE status='pending';
CREATE INDEX device_map_webhook_order ON device_map_webhook_work(map_id,id)
    WHERE status IN ('pending','sending');
CREATE INDEX device_map_webhook_sending ON device_map_webhook_work(claimed_at) WHERE status='sending';
CREATE INDEX device_map_webhook_finished ON device_map_webhook_work(finished_at)
    WHERE status IN ('accepted','failed','cancelled');
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='zenplus') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON sensor_transition_outbox,device_map_webhook_work TO zenplus;
        GRANT USAGE,SELECT ON SEQUENCE device_transition_queue_order,device_map_webhook_work_id_seq TO zenplus;
    END IF;
END $$;
COMMIT;
