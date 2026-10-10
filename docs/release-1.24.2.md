# ZenPlus 1.24.2 — Storage protection and legacy-upgrade rollback fixes

1.24.2 contains everything in 1.24.1 (bounded ClickHouse diagnostics, the free-space
watchdog, efficient agent telemetry, OS log caps and the rebuilt Storage page) and fixes how
its configuration and service changes are rolled back on supported versions (1.23.9 and
later). Use 1.24.2 instead of 1.24.1. Appliance-only: separately installed Windows agents are
untouched.

- **ClickHouse configuration outside the application tree.** The logging limits now live in
  `/etc/zenplus/clickhouse` and are staged, validated and replaced atomically, so a rollback
  that restores the application tree no longer removes files the ClickHouse container
  mounts. ClickHouse is re-created only when its configuration changed.
- **Service state restored on rollback.** Before anything is stopped, the update records the
  services it installs (NetFlow collector, NCM backup/delivery/worker, syslog receiver,
  storage watchdog). A rollback restores that recorded state: units the previous version
  did not have are removed, and units that were running are started again. If a bind-mount
  source left by an earlier release is missing, ClickHouse is re-created from the restored
  configuration.
- **Accurate Storage page.** Every ClickHouse diagnostic table shows its real size; switched-
  off logs and leftover copies show when they expire; copies taken during a storage recovery
  are shown as recovery evidence. A watchdog error clears only after ClickHouse is reachable
  again.
- **Retention of ClickHouse's own logs.** Leftover copies of ClickHouse's own logs and logs
  that are now switched off are removed automatically once older than their retention
  (3–30 days). Recovery evidence (`*_pre_recovery_*`), unknown or custom tables, detached
  parts and monitoring data are not removed by this cleanup.
- **Critical free space pauses bulk ingestion.** When the disk that stores monitoring data is
  critically full, host-agent and APM uploads receive retryable 503 responses until real free
  space returns, even while a purge is still in progress. How much data a client keeps while
  it retries depends on that client's own limits.

This update does not repair existing filesystem or ClickHouse data corruption, and does not
re-attach or delete detached parts. No new database migration files in this release; older
appliances still run the existing schema convergence. Appliance-only, minimum prerequisite
1.23.9. Details: `docs/storage-management.md`, `docs/storage-1.24.2-migration-acceptance.md`.
