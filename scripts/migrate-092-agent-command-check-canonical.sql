-- Migration 092: restore the canonical agent command constraint.
--
-- Some upgraded appliances baselined migrate-055 because its APM tables
-- already existed. The migration ledger was therefore updated without the
-- constraint rebuild at the end of that file, leaving APM commands rejected
-- by PostgreSQL even though the schema gate reported clean.
--
-- Keep this migration constraint-only so it can never be baselined from an
-- unrelated table or column. It is idempotent and safe for the schema gate to
-- replay when it detects definition drift.

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
        'apm_set_config'
    ));

COMMIT;
