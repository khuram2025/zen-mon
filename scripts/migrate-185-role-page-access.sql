-- migrate-185-role-page-access.sql
-- Custom page-access roles: limit a role to selected pages and network maps.
--
--   roles.page_access  NULL (the default, and every existing role after this
--                      migration) = the role is not limited: members keep
--                      exactly today's access. Otherwise
--                        {"pages": ["/devices", "/maps/manual", ...] | null,
--                         "maps":  ["<manual_maps.id>", ...]        | null}
--                      pages are sidebar pages (the navigation entry's
--                      address); null in either field = no limit there. A page
--                      list narrows the role's permissions to what the listed
--                      pages need, so the API of a hidden page is refused; the
--                      map list limits which network maps (NOC views) members
--                      can list and open. Full administration is never limited.
--
-- Idempotent: tolerates a re-run. No data is changed.

ALTER TABLE roles ADD COLUMN IF NOT EXISTS page_access JSONB;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roles_page_access_shape') THEN
        ALTER TABLE roles ADD CONSTRAINT roles_page_access_shape
            CHECK (page_access IS NULL OR jsonb_typeof(page_access) = 'object');
    END IF;
END $$;

COMMENT ON COLUMN roles.page_access IS
  'Page limit: {"pages": [nav addresses]|null, "maps": [manual map ids]|null}. NULL = not limited (every page and map the permissions allow). Narrows the role''s effective permissions to what the listed pages need; system.admin is never limited.';
