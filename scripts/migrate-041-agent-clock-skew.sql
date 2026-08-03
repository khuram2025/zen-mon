-- Agent clock skew visibility.
--
-- Metric batches whose timestamps deviate from server time are shifted to
-- server time at ingest (host_metric_service). The measured offset is stored
-- per agent so the fleet UI can flag hosts with a wrong clock/timezone —
-- skewed clocks silently break every "latest metrics" window and make
-- compliance timestamps untrustworthy.

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS clock_skew_s INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN agents.clock_skew_s IS
    'Agent wall-clock minus server time, seconds, from the last metric batch. Corrected at ingest.';
