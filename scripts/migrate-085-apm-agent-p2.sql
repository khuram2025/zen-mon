-- P2 agent-observed deployment markers for managed Windows services.

ALTER TABLE apm_agent_processes
    ADD COLUMN IF NOT EXISTS artifact_path text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS artifact_fingerprint varchar(64) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS artifact_modified_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_deployment_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_apm_agent_processes_deployment
    ON apm_agent_processes(agent_id, last_deployment_at DESC)
    WHERE artifact_fingerprint <> '';
