-- Migration 109: browser RUM funnels (RUM roadmap phase 4).
-- A funnel is an ordered list of steps (routes or user actions); results are
-- computed on demand from ClickHouse with windowFunnel, so only the
-- definition is stored here. Additive and idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS rum_funnels (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id   VARCHAR(128) NOT NULL,
    name             VARCHAR(255) NOT NULL,
    description      TEXT         NOT NULL DEFAULT '',
    -- [{"type": "view" | "action", "match": "/checkout" }, ...]; "*" in a
    -- view match is a wildcard segment, like route rules.
    steps            JSONB        NOT NULL,
    -- Steps must complete within this many seconds of the first step.
    window_seconds   INTEGER      NOT NULL DEFAULT 3600
                     CHECK (window_seconds BETWEEN 60 AND 604800),
    created_by       VARCHAR(255) NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (application_id, name)
);

COMMIT;
