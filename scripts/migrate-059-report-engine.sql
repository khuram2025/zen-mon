-- Migration 059: section-based report engine.
--
-- Adds user-defined custom reports (a named set of sections rendered by the
-- section registry in server/app/services/report_sections.py) and widens
-- report_schedules to cover the new section-based report types, longer
-- rolling periods, and scheduling of custom reports.

CREATE TABLE IF NOT EXISTS custom_reports (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(120) NOT NULL,
    description TEXT,
    sections    JSONB NOT NULL DEFAULT '[]'::jsonb,   -- ordered list of section ids
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Postgres auto-names inline column CHECKs <table>_<column>_check.
ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_report_type_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_report_type_check CHECK (
    report_type IN (
        -- legacy fpdf2 types
        'executive_summary','device_health','service_health','alert_analysis','full_report',
        -- section-engine presets
        'availability','performance','traffic','usage','apm_performance',
        'capacity','alerts','inventory',
        -- user-defined (requires custom_report_id)
        'custom'
    )
);

ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_period_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_period_check CHECK (
    period IN ('last_24h','last_7d','last_30d','last_90d')
);

ALTER TABLE report_schedules
    ADD COLUMN IF NOT EXISTS custom_report_id UUID REFERENCES custom_reports(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_custom_reports_created ON custom_reports (created_at DESC);

GRANT ALL ON custom_reports TO zenplus;
