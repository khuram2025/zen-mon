-- Migration 104: tag classification & per-user visibility scoping
--
-- Tags become the product's cross-entity classification: the same registry
-- (migrate-067) now covers applications (apm_services.tags, a column that
-- existed since migrate-039 but had no write path) and service checks
-- (service_checks.tags TEXT[], which predates the registry), alongside the
-- devices/servers/interfaces surfaces that were already wired in. The
-- propagation lists in app/services/tag_service.py grow matching entries so a
-- rename or delete can no longer strand stale names on those tables — with
-- tags now gating visibility, a missed rename would silently change who can
-- see what, which is why this lands in the same change.
--
-- users.scope_tags is the visibility scope: a JSONB text array of tag names.
-- Empty (the default) means unrestricted — every existing user keeps exactly
-- the view they have today. A non-empty scope means the user sees only
-- entities (devices, servers, service checks, APM services, and the alerts
-- attached to them) carrying at least one of those tags, matched
-- case-insensitively; untagged entities are hidden from scoped users, the
-- fail-closed default. Roles keep answering "what can you do", scope tags
-- answer "what can you see" — deliberately orthogonal, so one role serves
-- many teams. Users whose role carries system.admin are never scoped: an
-- admin cannot lock themselves out of the fleet.
--
-- Scope semantics on registry changes: a tag RENAME rewrites users.scope_tags
-- (access follows the tag); a tag DELETE leaves the name in place, where it
-- matches nothing — fail-closed. Stripping it could silently widen a user
-- whose last scope tag was deleted to unrestricted.
--
-- The GIN index mirrors idx_devices_tags for the service_checks TEXT[]
-- column, which the scoping predicate now filters on every list query.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS scope_tags JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN users.scope_tags IS
  'Visibility scope: tag names (JSONB text array) this user is limited to. Empty = unrestricted. Matched case-insensitively against entity tags; system.admin roles bypass. Renames propagate from /api/v1/tags; deletes deliberately do not (fail-closed).';

CREATE INDEX IF NOT EXISTS idx_service_checks_tags
  ON service_checks USING GIN (tags);

COMMIT;
