-- Allow the network-capture command.
--
-- agent_commands.command is whitelisted by a CHECK constraint; a new agent
-- command has to be added here or the insert fails at runtime rather than at
-- deploy time.

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
        'start_network_capture'
    ));
