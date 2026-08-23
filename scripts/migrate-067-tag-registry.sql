-- Migration 067: tag registry
--
-- Central catalog for the free-form labels stored in devices.tags (JSONB
-- text array). Assignments STAY on the device row — jsonb_exists() filters,
-- maintenance-window tag scoping and the GIN index all keep working — while
-- this table adds the metadata a real tagging system needs: a stable id,
-- canonical spelling (case-insensitive unique), a display color and an
-- optional description. The /api/v1/tags router manages it; device writes
-- auto-register unknown tags so CSV imports and API clients never 409.
--
-- Idempotent and safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS tags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(64) NOT NULL,
    -- '#rrggbb'. Always set: the API picks one from its palette when the
    -- caller doesn't, so every surface renders a tag the same way.
    color       VARCHAR(7),
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- "Core" and "core" are the same tag; the registry keeps one spelling.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_name_ci ON tags (LOWER(name));

-- Adopt any tags already living on devices (fleets that tagged devices
-- through Add/Edit Device or CSV import before the registry existed).
INSERT INTO tags (name, color)
SELECT t.name,
       (ARRAY['#6366f1','#0ea5e9','#10b981','#f59e0b','#ef4444',
              '#8b5cf6','#ec4899','#14b8a6','#f97316','#84cc16'])
       [1 + (ROW_NUMBER() OVER (ORDER BY t.name))::int % 10]
FROM (
    SELECT DISTINCT jsonb_array_elements_text(COALESCE(tags, '[]'::jsonb)) AS name
    FROM devices
) t
WHERE btrim(t.name) <> ''
  AND NOT EXISTS (SELECT 1 FROM tags x WHERE LOWER(x.name) = LOWER(t.name));

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON tags TO zenplus;
  END IF;
END $$;

COMMIT;
