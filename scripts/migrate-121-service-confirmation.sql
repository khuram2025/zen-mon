-- New-service defaults only: retain every existing retry threshold and delay.
ALTER TABLE service_checks ALTER COLUMN retry_count SET DEFAULT 3;
ALTER TABLE service_checks ALTER COLUMN retry_delay_s SET DEFAULT 10;
ALTER TABLE service_check_templates ALTER COLUMN default_retry_count SET DEFAULT 3;
ALTER TABLE service_check_templates ALTER COLUMN default_retry_delay_s SET DEFAULT 10;

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS down_check_interval INTEGER NOT NULL DEFAULT 30;
ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS verification_attempt SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE service_check_vantage_status ADD COLUMN IF NOT EXISTS verification_attempt SMALLINT NOT NULL DEFAULT 0;
