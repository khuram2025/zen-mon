-- migrate-197-alert-delivery-content.sql
-- What each alert notification said (alerting plan 2.5, defect T-01).
--
-- alert_deliveries (migrate-135) records who was sent which outcome: channel,
-- kind, outcome, error and latency. It did not record WHAT was sent, so a
-- complaint about a wrong SMS or email could not be checked afterwards.
--
--   subject           the email subject, or the title of a chat/webhook
--                     message, exactly as handed to the transport. NULL for
--                     SMS and for rows written before this migration.
--   content           the plain-text email body, the SMS text, or the text
--                     fields of a chat/webhook payload, after localisation and
--                     the SMS timestamp; capped at 8,000 characters by the
--                     writer. Never headers, URLs or channel secrets.
--   recipients_count  how many recipients the send addressed (one for each
--                     SMS row, the address count for an email). Recipient
--                     addresses and phone numbers are deliberately not stored.
--
-- subject and content are cleared (the row is kept) once they are older than
-- system_settings['alert_delivery_content'].days (default 30, 1-365).
-- Idempotent; existing rows keep NULL.

ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS recipients_count INTEGER;

-- The retention sweep finds rows that still hold a message, oldest first.
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_content_time
    ON alert_deliveries(attempted_at)
    WHERE subject IS NOT NULL OR content IS NOT NULL;
