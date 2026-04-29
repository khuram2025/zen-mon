-- Migration 007 — Multiple HTTP expected statuses
-- Adds a TEXT column that holds a comma-separated list of accepted status
-- patterns. Values: exact ("200"), wildcard ("2xx"), or range ("200-299").
-- The old http_expected_status INT stays for back-compat; the new column
-- takes precedence when non-empty.

ALTER TABLE service_checks
  ADD COLUMN IF NOT EXISTS http_expected_statuses TEXT;

ALTER TABLE service_check_templates
  ADD COLUMN IF NOT EXISTS http_expected_statuses TEXT;

-- Backfill from the existing int column for rows that don't have it set.
UPDATE service_checks
   SET http_expected_statuses = http_expected_status::text
 WHERE http_expected_statuses IS NULL
   AND http_expected_status IS NOT NULL;

UPDATE service_check_templates
   SET http_expected_statuses = http_expected_status::text
 WHERE http_expected_statuses IS NULL
   AND http_expected_status IS NOT NULL;
