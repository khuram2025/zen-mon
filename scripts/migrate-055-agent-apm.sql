-- Agent-side APM gateway, discovery inventory, and scoped ingest credentials.

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS apm_status jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS agent_apm_credentials (
    agent_id uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    key_id uuid NOT NULL UNIQUE REFERENCES apm_ingest_keys(id) ON DELETE CASCADE,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    rotated_at timestamptz
);

CREATE TABLE IF NOT EXISTS apm_agent_processes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    process_key varchar(128) NOT NULL,
    pid integer NOT NULL DEFAULT 0,
    ppid integer NOT NULL DEFAULT 0,
    exe_path text NOT NULL DEFAULT '',
    cmdline text NOT NULL DEFAULT '',
    runtime varchar(32) NOT NULL DEFAULT 'other'
        CONSTRAINT apm_agent_processes_runtime_check CHECK (
            runtime IN ('dotnet','dotnet_framework','java','node','python','iis','other')
        ),
    runtime_version varchar(128) NOT NULL DEFAULT '',
    service_name_guess varchar(255) NOT NULL DEFAULT '',
    windows_service varchar(255),
    iis_site varchar(255),
    iis_app_pool varchar(255),
    listening_ports jsonb NOT NULL DEFAULT '[]'::jsonb,
    instrumentation_state varchar(20) NOT NULL DEFAULT 'none'
        CONSTRAINT apm_agent_processes_instrumentation_state_check CHECK (
            instrumentation_state IN ('none','pending','active','failed','unsupported')
        ),
    otel_detected boolean NOT NULL DEFAULT false,
    otel_endpoint text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, process_key)
);

CREATE INDEX IF NOT EXISTS idx_apm_agent_processes_server
    ON apm_agent_processes(server_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_apm_agent_processes_runtime
    ON apm_agent_processes(runtime, instrumentation_state);

ALTER TABLE agent_commands DROP CONSTRAINT IF EXISTS agent_commands_command_check;
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
        'stop_network_capture',
        'apm_instrument',
        'apm_uninstrument',
        'apm_restart_target',
        'apm_set_config'
    ));
