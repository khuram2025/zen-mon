-- migrate-195-user-role-source.sql
-- Who decides the role of a directory account (LDAP / RADIUS / OIDC).
--
-- Until now the provider's role mapping overwrote users.role on every
-- sign-in and the user API refused a hand-set role, so an administrator
-- could not change the role of a directory user at all.
--
--   users.role_manual     true = an administrator set this account's role by
--                         hand; sign-in keeps it instead of applying the
--                         mapping. false (every existing account) = the
--                         mapping decides, exactly as before. Meaningless for
--                         local accounts, whose role is always set by hand.
--   users.directory_role  the role the provider's mapping gave at the last
--                         sign-in, recorded whether or not it was applied, so
--                         the page can show both and "follow the directory
--                         again" can apply it at once. NULL = not seen since
--                         this migration.
--
-- The directory keeps deciding WHETHER the account may sign in (it must
-- authenticate and match a mapping or the default role); role_manual only
-- decides WHICH role it holds. Idempotent; no existing value is changed.
--
-- Usernames are also made unique ignoring case in the database. The user API
-- already refused "JSmith" beside "jsmith", but the directory sign-in path
-- provisioned accounts with whatever spelling was typed, so a case variant
-- could become a second account. Sign-in now resolves usernames ignoring case;
-- the index makes that hold for every writer. Where case variants already
-- exist the index is skipped with a notice (nothing is merged or deleted) and
-- sign-in keeps treating the exact spelling as the account meant.

ALTER TABLE users ADD COLUMN IF NOT EXISTS role_manual BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS directory_role VARCHAR(50);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users GROUP BY lower(username) HAVING count(*) > 1) THEN
        RAISE NOTICE 'users_username_lower_key not created: usernames that differ only by case exist';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_key ON users (lower(username));
    END IF;
END $$;
