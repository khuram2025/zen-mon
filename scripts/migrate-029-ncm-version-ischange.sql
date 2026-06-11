-- NCM: store every backup snapshot, tagging whether it changed vs the prior
-- version. Previously unchanged snapshots were skipped (dedup); now they are
-- kept and flagged so the version list can show "Changed" / "No change".
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS is_change boolean NOT NULL DEFAULT true;

-- Existing rows were all real changes (unchanged pulls were never stored).
UPDATE device_configs SET is_change = true WHERE is_change IS NULL;
