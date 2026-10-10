# ZenPlus 1.24.2 — Storage protection, safe on every upgrade path

1.24.2 contains everything in 1.24.1 (bounded ClickHouse diagnostics, the free-space
watchdog, efficient agent telemetry, OS log caps and the rebuilt Storage page) and makes it
safe to install and to roll back from any supported version (1.23.9 and later). Use 1.24.2
instead of 1.24.1. Appliance-only: separately installed Windows agents are untouched.

- **Rollback-safe ClickHouse configuration.** The logging limits now live in
  `/etc/zenplus/clickhouse`, outside the application tree, and are staged, validated and
  replaced atomically. A failed upgrade's rollback can no longer leave ClickHouse unable to
  start. ClickHouse is re-created only when its configuration changed.
- **Rollback restores the previous host state.** Before anything is stopped, the update
  records the services it installs (NetFlow collector, NCM backup/delivery/worker, syslog
  receiver, storage watchdog). If the update fails, each is restored exactly: units the
  previous version did not have are removed (no restart loops or failed units), and units
  that were running are started again. A bind-mount problem left by an earlier release is
  repaired by re-creating ClickHouse from the restored configuration.
- **Accurate Storage page.** Every ClickHouse diagnostic table shows its real size; switched-
  off logs and leftover copies show when they expire; copies taken during a storage recovery
  are shown as recovery evidence. A watchdog error clears only after ClickHouse is reachable
  again.
- **Retention you can trust.** Leftover copies of ClickHouse's own logs and logs that are
  now switched off are removed automatically once older than their retention (3–30 days).
  Recovery evidence (`*_pre_recovery_*`), unknown or custom tables, detached parts and all
  monitoring data are never removed by this cleanup.
- **Critical free space pauses bulk ingestion immediately.** When the disk that stores
  monitoring data is critically full, host-agent and APM uploads pause (agents keep the
  data) until real free space returns, even while a purge is still in progress.

This update does not repair existing filesystem or ClickHouse data corruption, and does not
re-attach or delete detached parts. No database migrations. Appliance-only, minimum
prerequisite 1.23.9. Details: `docs/storage-management.md`,
`docs/storage-1.24.2-migration-acceptance.md`.
