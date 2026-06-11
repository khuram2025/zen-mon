-- Migration 031: discovery → server-monitoring import path.
--
-- Discovery results can now be imported as `servers` (server monitoring
-- module) in addition to `devices` (network monitoring). These columns track
-- the created server per result / import item, mirroring the device columns.

ALTER TABLE discovery_results_v2
    ADD COLUMN IF NOT EXISTS imported_server_id uuid REFERENCES servers(id) ON DELETE SET NULL;

ALTER TABLE discovery_import_items
    ADD COLUMN IF NOT EXISTS server_id uuid REFERENCES servers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_disc_results_imported_server
    ON discovery_results_v2 (imported_server_id);
