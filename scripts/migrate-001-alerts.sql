-- Migration: Enhanced alert rules and notification gateways
-- Run against existing database

-- Add new columns to alert_rules for intelligent alerting
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS device_type VARCHAR(50);
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS location VARCHAR(255);
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS trigger_on VARCHAR(20) DEFAULT 'any' CHECK (trigger_on IN ('any', 'down', 'up', 'degraded'));
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS recovery_alert BOOLEAN DEFAULT FALSE;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS min_duration INTEGER DEFAULT 0;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS max_repeat INTEGER DEFAULT 0;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS schedule_start TIME;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS schedule_end TIME;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS schedule_days JSONB DEFAULT '[1,2,3,4,5,6,7]';

-- Update notification_channels type constraint to include sms
ALTER TABLE notification_channels DROP CONSTRAINT IF EXISTS notification_channels_type_check;
ALTER TABLE notification_channels ADD CONSTRAINT notification_channels_type_check
    CHECK (type IN ('email', 'sms', 'webhook', 'slack', 'telegram'));

-- Add updated_at to notification_channels
ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Create system_settings table for global config (SMTP, SMS gateways)
CREATE TABLE IF NOT EXISTS system_settings (
    key         VARCHAR(100) PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  UUID REFERENCES users(id) ON DELETE SET NULL
);

-- Seed default gateway settings
INSERT INTO system_settings (key, value) VALUES
    ('smtp', '{"host":"","port":587,"username":"","password":"","from_email":"","from_name":"ZenPlus Alerts","encryption":"tls","enabled":false}'),
    ('sms', '{"provider":"twilio","account_sid":"","auth_token":"","from_number":"","enabled":false}')
ON CONFLICT (key) DO NOTHING;
