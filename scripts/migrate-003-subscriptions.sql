-- Migration 003: Add subscriptions table
-- Required for: Settings > Subscription tab

CREATE TABLE IF NOT EXISTS subscriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan                VARCHAR(50) NOT NULL DEFAULT 'trial',
    status              VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    max_devices         INTEGER NOT NULL DEFAULT 50,
    max_service_checks  INTEGER NOT NULL DEFAULT 20,
    max_users           INTEGER NOT NULL DEFAULT 5,
    license_key         VARCHAR(255),
    activated_by        VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
