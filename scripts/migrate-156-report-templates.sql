-- migrate-156-report-templates.sql
-- Report templates: organisation templates with per-section options, owners
-- and visibility; schedules that can run any catalog template, on calendar
-- periods, delivered to direct email recipients.
--
--   custom_reports       becomes the store for organisation report templates.
--                        definition  {version, sections:[{id, title?, options{}}]}
--                                    — ordered, a section may repeat with
--                                    different options; `sections` (ids) is kept
--                                    in step for older readers.
--                        visibility  private (owner only) | shared (everyone
--                                    with reports.view). Existing rows were
--                                    visible to everyone, so they become shared.
--                        source_key  the built-in template it was cloned from.
--   report_schedules     report_type accepts any catalog key (the API validates
--                        it against the built-in catalog), period gains
--                        calendar windows in the organisation timezone, format
--                        gains html, and recipients holds direct email
--                        addresses alongside notification channels.
--
-- Built-in templates live in code (app.services.report_templates) and are not
-- stored here. Idempotent: every statement tolerates a re-run.

BEGIN;

ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS definition     JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS category       VARCHAR(60);
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS tags           JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS visibility     VARCHAR(16) NOT NULL DEFAULT 'shared';
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS source_key     VARCHAR(80);
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS default_period VARCHAR(24);
ALTER TABLE custom_reports ADD COLUMN IF NOT EXISTS updated_by     UUID REFERENCES users(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'custom_reports'::regclass
                      AND conname = 'custom_reports_visibility_check') THEN
        ALTER TABLE custom_reports
            ADD CONSTRAINT custom_reports_visibility_check CHECK (visibility IN ('private', 'shared'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_custom_reports_owner ON custom_reports (created_by);

-- Report type: any catalog key. The list used to be repeated here, in the API
-- and in the catalog; the API now validates against the catalog alone.
ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_report_type_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_report_type_check
    CHECK (report_type ~ '^[a-z][a-z0-9_]{0,49}$');

-- Rolling windows plus calendar windows in the organisation timezone.
ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_period_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_period_check CHECK (period IN (
    'last_24h', 'last_7d', 'last_30d', 'last_90d',
    'today', 'yesterday', 'week_to_date', 'previous_week',
    'month_to_date', 'previous_month', 'quarter_to_date', 'previous_quarter'
));

ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_format_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_format_check
    CHECK (format IN ('pdf', 'excel', 'csv', 'html', 'none'));

ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS recipients JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN custom_reports.definition IS
    'Report template: {version, sections:[{id, title?, options{}}]} — ordered section entries.';
COMMENT ON COLUMN custom_reports.visibility IS
    'private: only the owner (and report managers) see it; shared: everyone with reports.view.';
COMMENT ON COLUMN report_schedules.recipients IS
    'Direct email recipients (in addition to notification channels).';

COMMIT;
