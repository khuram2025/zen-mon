-- Agent capabilities and complete on-demand capture lifecycle.

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_capabilities_array_check;
ALTER TABLE agents ADD CONSTRAINT agents_capabilities_array_check
    CHECK (jsonb_typeof(capabilities) = 'array');

CREATE INDEX IF NOT EXISTS idx_agents_capabilities
    ON agents USING GIN (capabilities);

ALTER TABLE network_captures
    DROP CONSTRAINT IF EXISTS network_captures_status_check;
ALTER TABLE network_captures ADD CONSTRAINT network_captures_status_check
    CHECK (status IN (
        'queued', 'running', 'stopping', 'completed',
        'failed', 'expired', 'cancelled'
    ));

ALTER TABLE agent_commands
    DROP CONSTRAINT IF EXISTS agent_commands_command_check;
ALTER TABLE agent_commands ADD CONSTRAINT agent_commands_command_check
    CHECK (command IN (
        'status',
        'collect_now',
        'refresh_config',
        'upload_diagnostics',
        'rotate_certificate',
        'restart_agent',
        'upgrade_agent',
        'start_network_capture',
        'stop_network_capture'
    ));

-- Stopping is still active: the host may continue collecting until it
-- acknowledges the stop command, so another capture must not start yet.
DROP INDEX IF EXISTS idx_network_captures_active;
CREATE INDEX idx_network_captures_active
    ON network_captures (status)
    WHERE status IN ('queued', 'running', 'stopping');

DROP INDEX IF EXISTS idx_network_captures_one_active;

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY server_id ORDER BY requested_at) AS rn
    FROM network_captures
    WHERE status IN ('queued', 'running', 'stopping')
)
UPDATE network_captures c
SET status = 'failed',
    completed_at = COALESCE(c.completed_at, NOW()),
    error_message = COALESCE(NULLIF(c.error_message, ''),
                             'Superseded by another active capture on the same server.'),
    updated_at = NOW()
FROM ranked
WHERE c.id = ranked.id AND ranked.rn > 1;

CREATE UNIQUE INDEX idx_network_captures_one_active
    ON network_captures (server_id)
    WHERE status IN ('queued', 'running', 'stopping');
