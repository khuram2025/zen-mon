-- migrate-070-udt-classification.sql
-- UDT endpoint classification override rules ("group by MAC/vendor").
--
-- The sweeper classifies endpoints heuristically from OUI vendor and
-- hostname. Operators need to override that in bulk ("MACs starting
-- 00:17:23 are access-control readers") and to define custom groups
-- beyond the built-in types.
--
--   udt_class_rules           match (mac / mac_prefix / vendor / ...) ->
--                             assign a type/group. Lowest priority number
--                             wins when several rules match.
--   udt_endpoints.type_source provenance of endpoint_type:
--                               'auto'   heuristic, sweeper may refresh
--                               'rule'   set by a classification rule
--                               'manual' pinned by an operator, never
--                                        auto-managed

BEGIN;

CREATE TABLE IF NOT EXISTS udt_class_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    priority    INTEGER NOT NULL DEFAULT 100,
    match_type  VARCHAR(20) NOT NULL CHECK (match_type IN
                  ('mac', 'mac_prefix', 'ip', 'ip_range', 'subnet',
                   'hostname', 'vendor', 'user')),
    pattern     VARCHAR(255) NOT NULL,
    set_type    VARCHAR(30) NOT NULL,
    description TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_udt_class_rules_priority
    ON udt_class_rules (priority, created_at) WHERE enabled;

ALTER TABLE udt_endpoints
    ADD COLUMN IF NOT EXISTS type_source VARCHAR(10) NOT NULL DEFAULT 'auto';

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'udt_endpoints_type_source_check') THEN
    ALTER TABLE udt_endpoints ADD CONSTRAINT udt_endpoints_type_source_check
      CHECK (type_source IN ('auto', 'rule', 'manual'));
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON udt_class_rules TO zenplus;
    GRANT SELECT, INSERT, UPDATE, DELETE ON udt_endpoints TO zenplus;
  END IF;
END $$;

COMMIT;
