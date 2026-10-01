-- NetFlow rollups: make collector retries idempotent all the way down.
--
-- The collector sends a stable insert_deduplication_token per batch together
-- with deduplicate_blocks_in_dependent_materialized_views = 1, and migrate-137
-- gave flow_records the non-replicated block log, so a retried batch (an
-- ambiguous insert failure, or a spool replay after a restart) is skipped in
-- flow_records. ClickHouse 24.10 still pushes the retried block through the
-- materialized views, though, and each target table only skips it when it has
-- a block log of its own. Without this file every retry is counted twice in
-- the hourly conversation and QoS rollups, the 5-minute traffic and
-- interface rollups and the exporter-minutes table, while raw stays exact.
--
-- With the log on, an insert into these tables that carries no token is
-- deduplicated by content. The rollup healer (app/services/netflow_rollup.py)
-- rebuilds an hour with DELETE + INSERT ... SELECT, and an unchanged hour
-- produces a byte-identical block, which the log would silently drop and leave
-- the hour empty. The healer therefore inserts with insert_deduplicate = 0;
-- it ships in the same release as this file.
--
-- Metadata-only and replayable: MODIFY SETTING rewrites no data, and running
-- it again sets the same value. scripts/ch_migrate.py verifies the setting
-- (TABLE_SETTING_INVARIANTS), so an appliance whose ledger records this file
-- without the setting is healed by re-running it.

ALTER TABLE zenplus.flow_conversations_1h
    MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE zenplus.flow_qos_1h
    MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE zenplus.flow_traffic_5m
    MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE zenplus.flow_interface_5m
    MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE zenplus.flow_exporter_minutes
    MODIFY SETTING non_replicated_deduplication_window = 1000;
