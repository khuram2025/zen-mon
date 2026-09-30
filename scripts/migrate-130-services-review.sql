-- Migration 130: Services review — ownership, incident notes, maintenance lifecycle.
--
-- * service_checks.owner_team / runbook_url: operational ownership shown on
--   the detail page and carried by the portable export.
-- * service_check_notes: incident notes / assignment per check, optionally
--   tied to the start of a confirmed incident.
-- * service_check_maintenance lifecycle: ending a window early records its
--   effective end (ended_at/ended_by/end_reason) instead of deleting the row,
--   and a window that never started is cancelled (cancelled_at). Historical
--   SLA reports need the record to stay reproducible. Effective end is
--   LEAST(ends_at, ended_at); cancelled windows never apply.
--
-- Idempotent.

BEGIN;

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS owner_team VARCHAR(255);
ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS runbook_url TEXT;

CREATE TABLE IF NOT EXISTS service_check_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_check_id    UUID NOT NULL REFERENCES service_checks(id) ON DELETE CASCADE,
    incident_started_at TIMESTAMPTZ NULL,
    assignee            VARCHAR(255) NULL,
    body                TEXT NOT NULL,
    author_id           UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    author_name         VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_service_check_notes_check
    ON service_check_notes (service_check_id, created_at DESC);

ALTER TABLE service_check_maintenance ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ NULL;
ALTER TABLE service_check_maintenance ADD COLUMN IF NOT EXISTS ended_by UUID NULL;
ALTER TABLE service_check_maintenance ADD COLUMN IF NOT EXISTS end_reason TEXT NULL;
ALTER TABLE service_check_maintenance ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ NULL;
ALTER TABLE service_check_maintenance ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_service_check_maintenance_window
    ON service_check_maintenance (starts_at, ends_at)
    WHERE cancelled_at IS NULL;

-- History reads of one check's configuration changes.
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource
    ON audit_logs (resource_type, resource_id, created_at DESC);

COMMIT;
