-- migrate-074-rbac-roles.sql
-- Role-based access control: database-backed roles with per-module
-- permission lists, replacing the hardcoded role vocabulary.
--
--   roles                one row per role; `permissions` is a JSONB array
--                        of dotted permission ids ("devices.manage").
--                        "system.admin" implies every other permission.
--                        is_system roles cannot be renamed or deleted.
--   users.role           stores roles.name by value (app-enforced; system
--                        roles are rename-proof so no FK is needed).
--   users.auth_source    'local' | 'ldap' | 'radius' — where the account
--                        authenticates. External accounts have no usable
--                        local password.
--
-- The users_role_check CHECK from init-postgres.sql only allowed
-- admin|editor|viewer and predates the app's own role list; it is dropped
-- so DB-defined roles work. Legacy 'editor' users become 'operator'.
--
-- The permission vocabulary is owned by server/app/core/permissions.py;
-- the seeds below are a snapshot of the built-in roles at introduction.

BEGIN;

CREATE TABLE IF NOT EXISTS roles (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    permissions  JSONB NOT NULL DEFAULT '[]',
    is_system    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50);
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_source VARCHAR(20) NOT NULL DEFAULT 'local';

UPDATE users SET role = 'operator' WHERE role = 'editor';

INSERT INTO roles (name, display_name, description, permissions, is_system) VALUES
  ('admin', 'Administrator',
   'Full system access. Manage users, roles, settings, devices, alerts, and all configurations.',
   '["system.admin", "dashboard.view", "devices.view", "devices.manage",
     "discovery.view", "discovery.run", "alerts.view", "alerts.acknowledge",
     "alerts.manage", "service_checks.view", "service_checks.manage",
     "netflow.view", "udt.view", "udt.manage", "ncm.view", "ncm.manage",
     "apm.view", "apm.manage", "reports.view", "reports.export",
     "reports.manage", "maps.view", "maps.manage", "users.view",
     "users.manage", "roles.manage", "audit.view", "settings.view",
     "settings.manage"]'::jsonb,
   TRUE),
  ('operator', 'Operator',
   'Manage devices, discovery, alerts, service checks, and monitoring configuration. Cannot manage users, roles, or system settings.',
   '["dashboard.view", "devices.view", "devices.manage", "discovery.view",
     "discovery.run", "alerts.view", "alerts.acknowledge", "alerts.manage",
     "service_checks.view", "service_checks.manage", "netflow.view",
     "udt.view", "udt.manage", "ncm.view", "ncm.manage", "apm.view",
     "apm.manage", "reports.view", "reports.export", "maps.view",
     "maps.manage", "settings.view"]'::jsonb,
   TRUE),
  ('viewer', 'Viewer',
   'View dashboards, devices, and alerts. Can acknowledge alerts but cannot modify configurations.',
   '["dashboard.view", "devices.view", "discovery.view", "alerts.view",
     "alerts.acknowledge", "service_checks.view", "netflow.view",
     "udt.view", "ncm.view", "apm.view", "reports.view", "reports.export",
     "maps.view"]'::jsonb,
   TRUE),
  ('read_only', 'Read Only',
   'Read-only access to dashboards and reports. No modification permissions.',
   '["dashboard.view", "devices.view", "alerts.view", "service_checks.view",
     "netflow.view", "udt.view", "apm.view", "reports.view",
     "maps.view"]'::jsonb,
   TRUE)
ON CONFLICT (name) DO NOTHING;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON roles TO zenplus;
  END IF;
END $$;

COMMIT;
