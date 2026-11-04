CREATE TABLE IF NOT EXISTS virtualization_connectors (
    id UUID PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    provider VARCHAR(30) NOT NULL,
    endpoint TEXT NOT NULL,
    config JSONB NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]',
    enabled BOOLEAN NOT NULL DEFAULT true,
    interval_seconds INTEGER NOT NULL DEFAULT 300 CHECK (interval_seconds BETWEEN 60 AND 3600),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    last_error TEXT,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary JSONB NOT NULL DEFAULT '{}',
    snapshot JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS virtualization_connectors_due ON virtualization_connectors(next_poll_at) WHERE enabled;
