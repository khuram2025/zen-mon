-- migrate-150-access-control-hardening.sql
-- Users, Roles & Authentication review (RP-01..RP-25, UA-01..UA-16).
--
--   roles.revision / definition_version   optimistic concurrency for role edits and
--                                         versioned built-in role definitions
--   role_history                          before/after permission diff per change
--   users.role -> roles.name FK           referential integrity; renames cascade,
--                                         deleting an assigned role is refused
--   users lifecycle columns               forced password change, lockout counters,
--                                         TOTP multi-factor state, external identity id
--   auth_sessions                         server-side session inventory / revocation
--   netflow_saved_views.is_shared         personal vs shared views (existing views stay
--                                         visible to everyone, as before)
--
-- Idempotent: every statement tolerates a re-run.

BEGIN;

-- ── roles ────────────────────────────────────────────────────────────────────
ALTER TABLE roles ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS definition_version INTEGER;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS updated_by TEXT;

CREATE TABLE IF NOT EXISTS role_history (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id           UUID NOT NULL,
    role_name         TEXT NOT NULL,
    revision          INTEGER NOT NULL,
    action            TEXT NOT NULL,
    actor_id          UUID,
    actor_username    TEXT,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added             JSONB NOT NULL DEFAULT '[]'::jsonb,
    removed           JSONB NOT NULL DEFAULT '[]'::jsonb,
    permissions_after JSONB NOT NULL DEFAULT '[]'::jsonb,
    affected_users    INTEGER NOT NULL DEFAULT 0,
    reason            TEXT,
    changes           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_role_history_role ON role_history (role_id, occurred_at DESC);

-- ── users: lifecycle, lockout, MFA ───────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_failed_login TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_recovery_hashes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enrolled_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_last_counter BIGINT;
-- Stable identifier from an external identity provider (OIDC "sub"), so an
-- account is matched by identity, never by a username claim that can change.
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_identity ON users (auth_source, external_id)
    WHERE external_id IS NOT NULL;

-- Referential integrity for role assignment. NOT VALID enforces the rule for
-- every new or changed assignment without scanning (or failing on) legacy
-- rows; the VALIDATE below promotes it when no orphan exists. ON UPDATE
-- CASCADE makes a role rename atomic; ON DELETE RESTRICT closes the
-- assign-vs-delete race.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_role_fk') THEN
        ALTER TABLE users
            ADD CONSTRAINT users_role_fk FOREIGN KEY (role) REFERENCES roles (name)
            ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;
    END IF;
    BEGIN
        ALTER TABLE users VALIDATE CONSTRAINT users_role_fk;
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'users_role_fk left NOT VALID: some users reference a role that does not exist';
    END;
END $$;

-- ── server-side sessions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth_sessions (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    ip_address    TEXT,
    user_agent    TEXT,
    auth_method   TEXT NOT NULL DEFAULT 'password',
    mfa_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at    TIMESTAMPTZ,
    revoked_by    TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions (expires_at);

-- ── NetFlow saved views: personal vs shared ──────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.netflow_saved_views') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'netflow_saved_views' AND column_name = 'is_shared') THEN
        ALTER TABLE netflow_saved_views ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT FALSE;
        -- Until now every view was visible to every user: keep it that way.
        UPDATE netflow_saved_views SET is_shared = TRUE;
    END IF;
END $$;

COMMIT;
