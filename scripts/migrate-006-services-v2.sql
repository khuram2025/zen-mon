-- Migration 006 — Services feature v2 (M2-M7 + alert integration)
-- Postgres. Safe to re-run.

-- ─── service_checks extensions ────────────────────────────────────────────
ALTER TABLE service_checks
  ADD COLUMN IF NOT EXISTS level          SMALLINT NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS config         JSONB    NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS tags           TEXT[]   NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS retry_count    SMALLINT NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS retry_delay_s  SMALLINT NOT NULL DEFAULT 30,
  ADD COLUMN IF NOT EXISTS group_id       UUID,
  ADD COLUMN IF NOT EXISTS parent_check_id UUID REFERENCES service_checks(id) ON DELETE SET NULL;

-- Seed level based on existing check_type where unset.
UPDATE service_checks SET level = CASE
  WHEN check_type IN ('http','tls','dns','snmp_oid') THEN 2
  WHEN check_type IN ('tcp','icmp') THEN 1
  ELSE 1 END
WHERE level = 1;

CREATE INDEX IF NOT EXISTS idx_service_checks_parent ON service_checks(parent_check_id);

-- ─── service_check_groups ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_check_groups (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        VARCHAR(120) NOT NULL UNIQUE,
  description TEXT,
  color       VARCHAR(20),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Now that the groups table exists, add the FK on service_checks.group_id.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'service_checks_group_id_fkey' AND table_name = 'service_checks'
  ) THEN
    ALTER TABLE service_checks
      ADD CONSTRAINT service_checks_group_id_fkey
      FOREIGN KEY (group_id) REFERENCES service_check_groups(id) ON DELETE SET NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_service_checks_group_id ON service_checks(group_id);

-- ─── service_check_templates ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_check_templates (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            VARCHAR(120) NOT NULL UNIQUE,
  description     TEXT,
  check_type      VARCHAR(20) NOT NULL CHECK (check_type IN ('http','tcp','tls','icmp','dns')),
  level           SMALLINT NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 3),
  config          JSONB NOT NULL DEFAULT '{}'::jsonb,
  tags            TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  default_interval        INT NOT NULL DEFAULT 60 CHECK (default_interval BETWEEN 10 AND 3600),
  default_timeout         INT NOT NULL DEFAULT 10 CHECK (default_timeout BETWEEN 1 AND 60),
  default_retry_count     SMALLINT NOT NULL DEFAULT 1,
  default_retry_delay_s   SMALLINT NOT NULL DEFAULT 30,
  target_url_template     VARCHAR(2048),
  target_port_default     INT,
  http_method             VARCHAR(10) DEFAULT 'GET',
  http_expected_status    INT DEFAULT 200,
  http_content_match      VARCHAR(1024),
  http_follow_redirects   BOOLEAN DEFAULT TRUE,
  tls_warn_days           INT DEFAULT 30,
  tls_critical_days       INT DEFAULT 7,
  created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── service_check_maintenance ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_check_maintenance (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type      VARCHAR(20) NOT NULL CHECK (scope_type IN ('check','group','tag','all')),
  scope_check_id  UUID REFERENCES service_checks(id) ON DELETE CASCADE,
  scope_group_id  UUID REFERENCES service_check_groups(id) ON DELETE CASCADE,
  scope_tag       VARCHAR(120),
  starts_at       TIMESTAMPTZ NOT NULL,
  ends_at         TIMESTAMPTZ NOT NULL,
  reason          TEXT,
  created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_scm_active ON service_check_maintenance(starts_at, ends_at);

-- ─── alert_rules — service-check scoping + missing template columns ───────
ALTER TABLE alert_rules
  ADD COLUMN IF NOT EXISTS service_check_id        UUID REFERENCES service_checks(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS service_check_group_id  UUID REFERENCES service_check_groups(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS email_subject           TEXT,
  ADD COLUMN IF NOT EXISTS email_body              TEXT,
  ADD COLUMN IF NOT EXISTS sms_template            TEXT,
  ADD COLUMN IF NOT EXISTS recovery_email_subject  TEXT,
  ADD COLUMN IF NOT EXISTS recovery_email_body     TEXT,
  ADD COLUMN IF NOT EXISTS recovery_sms_template   TEXT,
  ADD COLUMN IF NOT EXISTS min_duration            INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_repeat              INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS schedule_start          TIME,
  ADD COLUMN IF NOT EXISTS schedule_end            TIME,
  ADD COLUMN IF NOT EXISTS schedule_days           JSONB,
  ADD COLUMN IF NOT EXISTS trigger_on              VARCHAR(20) DEFAULT 'any',
  ADD COLUMN IF NOT EXISTS recovery_alert          BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS device_type             VARCHAR(40),
  ADD COLUMN IF NOT EXISTS location                VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_alert_rules_svc_check  ON alert_rules(service_check_id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_svc_group  ON alert_rules(service_check_group_id);

-- Allow 'service_status' metric; broaden operator CHECK to accept symbol aliases.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_metric_check') THEN
    ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_metric_check;
  END IF;
END $$;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_metric_check
  CHECK (metric IN ('ping_status','rtt','packet_loss','jitter','service_status'));

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_operator_check') THEN
    ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_operator_check;
  END IF;
END $$;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_operator_check
  CHECK (operator IN ('eq','neq','gt','lt','gte','lte','>','<','>=','<=','==','!='));

-- ─── alerts — allow service-check-scoped rows ────────────────────────────
ALTER TABLE alerts
  ALTER COLUMN device_id DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS service_check_id UUID REFERENCES service_checks(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_alerts_svc_check ON alerts(service_check_id);
