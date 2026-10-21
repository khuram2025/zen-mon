-- Migration 166: report delivery — retries, stored artifacts, expiring share
-- links, email options and the "previous work week" period.
--
-- Field assessment of the SMASCO appliance (docs/reports-review-assessment.md,
-- docs/reports-delivery.md):
--
--  * report_runs keeps what a run sent (subject, bodies, attachment) so a
--    failed delivery can be retried exactly and the attachment downloaded from
--    the run history; failed_targets / retry_count / next_retry_at drive the
--    retries; window_from / window_to / kind / notes describe the run.
--  * Share links used to live forever: runs without an expiry get
--    generated_at + 30 days (the new default link lifetime).
--  * report_schedules gains the email options (subject template, reply-to,
--    one-message group send with CC) and skip_when_empty.
--  * The period check gains 'previous_work_week'.
--
-- Additive and idempotent.

BEGIN;

ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS kind           VARCHAR(16) NOT NULL DEFAULT 'scheduled';
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS window_from    TIMESTAMPTZ;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS window_to      TIMESTAMPTZ;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS email_subject  TEXT;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS email_html     TEXT;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS email_text     TEXT;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS artifact       BYTEA;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS artifact_name  VARCHAR(255);
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS artifact_mime  VARCHAR(120);
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS failed_targets JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS retry_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS next_retry_at  TIMESTAMPTZ;
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS notes          JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE report_runs SET expires_at = generated_at + INTERVAL '30 days' WHERE expires_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_report_runs_generated ON report_runs (generated_at);
CREATE INDEX IF NOT EXISTS idx_report_runs_retry
    ON report_runs (next_retry_at) WHERE next_retry_at IS NOT NULL;

ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS skip_when_empty  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS subject_template VARCHAR(255);
ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS reply_to         VARCHAR(254);
ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS group_send       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE report_schedules ADD COLUMN IF NOT EXISTS cc               JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE report_schedules DROP CONSTRAINT IF EXISTS report_schedules_period_check;
ALTER TABLE report_schedules ADD CONSTRAINT report_schedules_period_check CHECK (period IN (
    'last_24h', 'last_7d', 'last_30d', 'last_90d',
    'today', 'yesterday', 'week_to_date', 'previous_week', 'previous_work_week',
    'month_to_date', 'previous_month', 'quarter_to_date', 'previous_quarter'
));

COMMENT ON COLUMN report_runs.failed_targets IS
    'Delivery targets still owed this run: [{"kind": "channel"|"email", "id": ..., "error": ...}].';
COMMENT ON COLUMN report_runs.artifact IS
    'The attachment as sent; cleared after the artifact retention (system_settings reports).';
COMMENT ON COLUMN report_schedules.group_send IS
    'TRUE: one message to all direct recipients (visible to each other, cc applies); FALSE: one message each.';

COMMIT;
