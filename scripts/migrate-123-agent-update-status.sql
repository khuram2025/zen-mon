-- Agents from 1.14.1 report the progress of an upgrade requested by the
-- appliance (desired_version or the upgrade_agent command) in their
-- heartbeat: requested, downloading, installing, succeeded or failed, with
-- the reason for a failure. The latest report is kept per agent so Agent
-- Fleet can show it; an empty object means no upgrade has been reported.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS update_status jsonb NOT NULL DEFAULT '{}'::jsonb;
