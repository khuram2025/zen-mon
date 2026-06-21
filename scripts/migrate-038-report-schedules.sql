-- migrate-038-report-schedules.sql
-- Scheduled reports: periodic generation + delivery of HTML/PDF reports to
-- notification channels, plus a shareable rendered-HTML artifact per run.
--
-- Idempotent: safe to re-run. Apply as the postgres OS user (peer auth);
-- objects are owned by postgres like the rest of the schema.

-- ---------------------------------------------------------------------------
-- report_schedules: a recurring (or one-off) report delivery definition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,

    -- What to generate
    report_type     VARCHAR(50) NOT NULL DEFAULT 'executive_summary'
                    CHECK (report_type IN ('executive_summary','device_health',
                                           'service_health','alert_analysis','full_report')),
    period          VARCHAR(20) NOT NULL DEFAULT 'last_24h'
                    CHECK (period IN ('last_24h','last_7d','last_30d')),
    -- Attachment format delivered alongside the HTML email summary.
    format          VARCHAR(10) NOT NULL DEFAULT 'pdf'
                    CHECK (format IN ('pdf','excel','csv','none')),
    -- Optional scoping filters: {device_ids:[], group_ids:[], locations:[], device_types:[]}
    filters         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- When to fire
    frequency       VARCHAR(20) NOT NULL DEFAULT 'daily'
                    CHECK (frequency IN ('daily','weekly','monthly')),
    hour            SMALLINT NOT NULL DEFAULT 8  CHECK (hour BETWEEN 0 AND 23),
    minute          SMALLINT NOT NULL DEFAULT 0  CHECK (minute BETWEEN 0 AND 59),
    day_of_week     SMALLINT CHECK (day_of_week BETWEEN 1 AND 7),   -- weekly (1=Mon)
    day_of_month    SMALLINT CHECK (day_of_month BETWEEN 1 AND 31), -- monthly

    -- Where to deliver: list of notification_channels.id (as text).
    notify_channels JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Bookkeeping
    last_run_at     TIMESTAMPTZ,
    last_status     VARCHAR(20),         -- 'success' | 'partial' | 'failed'
    last_error      TEXT,
    next_run_at     TIMESTAMPTZ,
    created_by      UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_report_schedules_due
    ON report_schedules (next_run_at) WHERE enabled = TRUE;

-- ---------------------------------------------------------------------------
-- report_runs: one rendered report instance (scheduled or ad-hoc).
-- Holds the standalone HTML so the email "View full report" link can serve it
-- to recipients who are not logged in (token-gated, read-only).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id     UUID REFERENCES report_schedules(id) ON DELETE SET NULL,
    report_type     VARCHAR(50) NOT NULL,
    period          VARCHAR(20) NOT NULL,
    title           VARCHAR(255) NOT NULL,
    token           VARCHAR(64) NOT NULL UNIQUE,
    html            TEXT NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'success',
    delivered_to    JSONB NOT NULL DEFAULT '[]'::jsonb,   -- channel names emailed
    error           TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_report_runs_schedule
    ON report_runs (schedule_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_runs_token ON report_runs (token);
