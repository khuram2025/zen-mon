-- Migration 125: allow the set_monitoring_profile agent command.
--
-- Agent Fleet's "Change monitoring profile" (agents 1.14.2+) queues
-- set_monitoring_profile, which the canonical command constraint from
-- migration 092 rejected, so the request failed with HTTP 500.
--
-- Constraint-only and idempotent, like 092, so the migration runner can
-- replay it to heal definition drift. It replaces 092 as the canonical
-- definition in scripts/run-migrations.py.

BEGIN;

ALTER TABLE agent_commands
    DROP CONSTRAINT IF EXISTS agent_commands_command_check;

ALTER TABLE agent_commands
    ADD CONSTRAINT agent_commands_command_check
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
        'apm_set_config',
        'set_monitoring_profile'
    ));

COMMIT;
