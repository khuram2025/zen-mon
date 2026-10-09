-- Migration 158: Inventory & Compliance phase 1 follow-ups (data repairs and read-path indexes).
--
-- Migration 157 added the evidence columns and the capture triggers but left three things empty or
-- misleading on an appliance that already had data. This file repairs them. Nothing here changes the schema's
-- shape; every statement is idempotent, so a re-run (the updater may replay a migration to heal a gap) changes
-- nothing the first run did not.
--
--  1. The capture start. Change history cannot be backfilled, so the date capture began is the only honest
--     "history starts" date. It is stored once, as system_settings['inventory.capture_started_at'] (a JSON
--     string, UTC, ISO-8601 with microseconds and a Z: the repair in step 3 compares timestamps exactly), taken
--     from the time migration 157 was applied; migration 157 is what
--     armed the capture triggers. When 157 has no ledger row (a database built by other means) the time of this
--     migration is used. GET /inventory/changes returns it as history_start.
--
--  2. Freshness of servers that already reported. 157 left servers.inventory_updated_at NULL, so every server
--     read "Never" until its next agent cycle. A server that holds software rows was last heard from when it
--     last wrote one: max(server_software_inventory.updated_at) is exactly "the last time we received its
--     list" (every upload rewrites updated_at for every row of the list), so that is the stamp. Only servers
--     with a NULL stamp and a baselined software list are touched, and only once: the agent ingest owns the
--     column from then on.
--
--  3. "First seen" of software that pre-dates the capture. 157 added first_seen_at with DEFAULT now(), which
--     stamped every row that existed then with the migration time, so a product installed years ago read
--     "first seen" today. For those rows only (first_seen_at at or before the capture start) the install date
--     the machine itself reported is a better, honest lower bound: first_seen_at = LEAST(first_seen_at,
--     install_date) when install_date is a plausible past date (after 1995, before the stored first_seen_at).
--     Rows written after the capture start keep the time Inventory first saw them. One UPDATE: it rewrites at
--     most the rows that carry an install date (about 100 000 rows per second on the reference appliance, so
--     roughly 20 s at 2 million rows), inside this transaction, with the capture triggers suppressed (the
--     UPDATE sets first_seen_at only and so would not fire them anyway; the suppression is belt and braces).
--
--  4. Two small partial indexes for the read model. The security question ("endpoint protection present") is a
--     join on the few servers that have a software row of category 'endpoint-protection' or a running Defender
--     service; without an index each Overview build scans the whole software and service tables for it.
--     idx_servers_device_id serves the "which server owns this SNMP device" lookups of the change feed.

BEGIN;

SET LOCAL zenplus.inventory_suppress = 'on';

-- 1. capture start ---------------------------------------------------------

DO $$
DECLARE
    v_start timestamptz;
BEGIN
    IF to_regclass('schema_migrations') IS NOT NULL THEN
        EXECUTE 'SELECT applied_at FROM schema_migrations WHERE filename = $1'
            INTO v_start USING 'migrate-157-inventory-foundation.sql';
    END IF;
    v_start := COALESCE(v_start, now());
    INSERT INTO system_settings (key, value)
    VALUES ('inventory.capture_started_at',
            to_jsonb(to_char(v_start AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')))
    ON CONFLICT (key) DO NOTHING;
END $$;

-- 2. freshness of servers that already reported -------------------------------

UPDATE servers s
   SET inventory_updated_at = sw.newest
  FROM (SELECT i.server_id, max(i.updated_at) AS newest
          FROM server_software_inventory i
         GROUP BY i.server_id) sw
 WHERE sw.server_id = s.id
   AND s.inventory_updated_at IS NULL
   AND s.inventory_baselined_at IS NOT NULL;

-- 3. first seen of software that pre-dates the capture ------------------------

UPDATE server_software_inventory w
   SET first_seen_at = w.install_date
 WHERE w.install_date IS NOT NULL
   AND w.install_date >= timestamptz '1995-01-01 00:00:00+00'
   AND w.install_date < w.first_seen_at
   AND w.first_seen_at <= (SELECT (value #>> '{}')::timestamptz
                             FROM system_settings
                            WHERE key = 'inventory.capture_started_at');

-- 4. read-path indexes ------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_ssi_endpoint_protection
    ON server_software_inventory (server_id) WHERE category = 'endpoint-protection';

CREATE INDEX IF NOT EXISTS idx_ssvc_endpoint_protection
    ON server_service_inventory (server_id) WHERE service_name IN ('WinDefend', 'Sense');

CREATE INDEX IF NOT EXISTS idx_servers_device_id
    ON servers (device_id) WHERE device_id IS NOT NULL;

COMMIT;
