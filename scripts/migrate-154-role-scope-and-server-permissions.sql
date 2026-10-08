-- migrate-154-role-scope-and-server-permissions.sql
-- Team roles: role-level visibility scope, and Servers separated from APM.
--
--   roles.scope_tags   Tag names (JSONB text array) every member of the role is
--                      limited to. Empty (the default) = the role adds no limit,
--                      so every existing role keeps exactly today's view. A
--                      member's effective scope is the role's tags plus the
--                      user's own scope_tags, matched case-insensitively ANY-of;
--                      both empty = unrestricted. system.admin bypasses scope.
--                      Tag renames propagate here like users.scope_tags; tag
--                      deletes deliberately do not (fail-closed).
--
--   servers.view / servers.manage
--                      Servers, agents, agent policies and baselines used to be
--                      governed by apm.view / apm.manage, so an Application team
--                      could not be separated from a Systems team. Every role
--                      that held apm.view now also holds servers.view (and
--                      apm.manage -> servers.manage): nobody loses access on
--                      upgrade. Administrators can then remove whichever half a
--                      role should not have. Each changed role gets a revision
--                      bump and a role_history entry.
--
-- Built-in team roles (network_team, systems_team, application_team,
-- security_team) are created and kept current by the application at startup
-- (app.services.role_service), from the definitions in app.core.permissions.
--
-- Idempotent: every statement tolerates a re-run.

BEGIN;

ALTER TABLE roles ADD COLUMN IF NOT EXISTS scope_tags JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN roles.scope_tags IS
  'Visibility scope shared by every member: tag names (JSONB text array). Empty = no role-level limit. Unioned with users.scope_tags; system.admin bypasses. Renames propagate from /api/v1/tags; deletes deliberately do not (fail-closed).';

CREATE TEMP TABLE _role_server_split ON COMMIT DROP AS
SELECT r.id,
       r.permissions AS before,
       (SELECT COALESCE(jsonb_agg(p ORDER BY p), '[]'::jsonb)
          FROM (SELECT DISTINCT p
                  FROM (SELECT jsonb_array_elements_text(r.permissions) AS p
                        UNION ALL SELECT 'servers.view' WHERE r.permissions ? 'apm.view'
                        UNION ALL SELECT 'servers.manage' WHERE r.permissions ? 'apm.manage') granted
               ) distinct_granted) AS after
  FROM roles r
 WHERE jsonb_typeof(r.permissions) = 'array'
   AND ((r.permissions ? 'apm.view' AND NOT r.permissions ? 'servers.view')
     OR (r.permissions ? 'apm.manage' AND NOT r.permissions ? 'servers.manage'));

UPDATE roles r
   SET permissions = s.after,
       revision = r.revision + 1,
       updated_at = NOW(),
       updated_by = 'system'
  FROM _role_server_split s
 WHERE r.id = s.id;

INSERT INTO role_history (role_id, role_name, revision, action, actor_username, added, removed,
                          permissions_after, affected_users, reason, changes)
SELECT r.id, r.name, r.revision, 'permission_split', 'system',
       (SELECT COALESCE(jsonb_agg(p ORDER BY p), '[]'::jsonb)
          FROM jsonb_array_elements_text(s.after) p
         WHERE NOT s.before ? p),
       '[]'::jsonb,
       s.after,
       (SELECT count(*) FROM users u WHERE u.role = r.name),
       'Server permissions separated from APM: existing apm access carried over to servers.view / servers.manage',
       '{"migration": "migrate-154"}'::jsonb
  FROM _role_server_split s
  JOIN roles r ON r.id = s.id
 WHERE NOT EXISTS (SELECT 1 FROM role_history h
                    WHERE h.role_id = r.id AND h.action = 'permission_split' AND h.revision = r.revision);

COMMIT;
