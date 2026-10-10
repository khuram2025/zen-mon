-- Migration 159: audit_logs.actor_role holds any role name.
--
-- audit_logs.actor_role was created as VARCHAR(20) (migration 010) while roles.name and users.role allow 50
-- characters (a custom role created from the display name "Regional Security Operations" is stored as
-- regional_security_operations, 28 characters). The audit writer is best-effort, so for every role whose name is
-- longer than 20 characters the insert failed with "value too long for type character varying(20)", the error was
-- logged, and the action went unaudited. Phase 1 of Inventory & Compliance adds two actions that promise an audit
-- entry (inventory.export and inventory.settings.update) and refuses to proceed without it, so the column has to
-- be as wide as the role name it records.
--
-- Widening a VARCHAR is a catalog-only change in PostgreSQL (no table rewrite, no index rebuild; measured at 6 ms
-- on 300 000 rows) and nothing depends on the column (no view, no generated column). The statement runs only when
-- the column is still narrower than 50, so a replay changes nothing.

DO $$
DECLARE
    width integer;
BEGIN
    SELECT character_maximum_length INTO width
      FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = 'audit_logs' AND column_name = 'actor_role';
    IF width IS NOT NULL AND width < 50 THEN
        ALTER TABLE audit_logs ALTER COLUMN actor_role TYPE VARCHAR(50);
    END IF;
END
$$;
