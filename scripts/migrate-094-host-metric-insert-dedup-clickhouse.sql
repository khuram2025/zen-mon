-- Make /api/v1/agents/results/host retries idempotent per metric kind.
--
-- The API deliberately returns 503 when any authoritative store fails so the
-- Windows agent retains and replays its spool batch.  ClickHouse tables are
-- independent, however: CPU can commit before a later memory insert fails.
-- Enabling the non-replicated block log, together with the stable
-- insert_deduplication_token supplied by host_metric_service, makes that replay
-- a no-op for kinds that already committed instead of double-counting them.

ALTER TABLE zenplus.host_cpu_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_memory_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_filesystem_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_disk_io_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_network_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_process_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_service_state
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.host_event_log_summary
    MODIFY SETTING non_replicated_deduplication_window = 10000;
ALTER TABLE zenplus.agent_health_metrics
    MODIFY SETTING non_replicated_deduplication_window = 10000;
