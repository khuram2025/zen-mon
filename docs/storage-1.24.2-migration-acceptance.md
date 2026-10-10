# Storage 1.24.2 — legacy → candidate migration acceptance plan

Scope: appliances on the oldest supported OTA base (1.23.9) through 1.24.1 upgrading to
the 1.24.2 candidate. Monitoring data, detached/corrupt parts and recovery evidence must be
preserved. This update does **not** repair filesystem corruption and must not be described
as doing so.

## What the candidate does on upgrade (setup-storage.sh, manifest step "run_hook")

1. Root storage helpers + sudoers (visudo-validated), OS log caps (journald 1G/4G free,
   rsyslog `maxsize 500M` where rsyslog exists, hourly logrotate).
2. Installs and starts `zenplus-storage-watchdog` **before** touching ClickHouse
   (`ConditionPathExists` guards against a missing module after rollback).
3. `install-clickhouse-config.sh`: stages, validates and atomically renames
   `/etc/zenplus/clickhouse/config.d/zenplus_logging.xml` and
   `users.d/zenplus_profile.xml` (outside the code tree, so rollback never removes them).
4. Re-creates the ClickHouse container when the mount sources/destinations, network or
   configuration content differ; waits up to 300 s for health (hook timeout 600 s),
   otherwise fails the update → rollback.
5. On ClickHouse start, system logs whose definition changed are renamed to `<log>_<n>` and
   re-created with daily partitions + TTL on their next write. Disabled writers
   (`processors_profile_log`, `query_views_log`) are not re-created; they are treated as
   legacy. Legacy copies of **known** diagnostic logs are dropped once their newest day is
   past that log's retention (3/7/30 days), hourly, idempotently. `*_pre_recovery_*`
   copies are recovery evidence: never deleted automatically. Unknown/custom `system`
   tables are never classified or touched. Detached parts are never touched.

## What a failed upgrade's rollback does (any updater ≥ 1.23.9)

Manifest `rollback_steps` run from the signed payload: stop services →
`ota-restore-backup.py` → `enforce-support-security-floor.sh` →
**`ota-reconcile-storage-rollback.sh`** → start services → health.
Right after the backup, `ota-snapshot-units.py` records every systemd unit the package
installs (`install_systemd` steps: NetFlow collector, NCM backup/delivery/worker, syslog;
plus the storage watchdog): present/absent, enabled, active, and a copy of the file, tagged
with the package version. The reconcile hook restores that exact state: units that existed
are put back (file, enablement, and started again if they were active); units that did not
exist (e.g. collector/NCM worker/watchdog on 1.23.9) are disabled, stopped, removed and
reset-failed, so the rollback `start_services` step skips them. Without a matching
snapshot it removes units whose executable or Python module is missing from the restored
tree. It also removes a stale ingest-pause marker, restarts a kept watchdog on the restored
code, and re-creates ClickHouse from the restored compose file only if a bind-mount source
is missing (or replaced by a directory).
Kept intentionally: `/etc/zenplus/clickhouse` (container keeps bounded logging until
re-created), journald/logrotate caps, root helpers and sudoers entries, renamed tables.

## Acceptance matrix (lab clone `legacy-1.23.9-storage-fixtures-20261010`)

Record before and after every phase: `.version`; `df -B1 /`; ext4 `errors_count`;
`systemctl is-active` (api, poller, netflow, watchdog, docker, postgresql);
`SELECT database, table, sum(rows), count(), sum(bytes_on_disk) FROM system.parts
WHERE active GROUP BY 1,2`; `SELECT count() FROM system.detached_parts`;
`SELECT name, engine_full FROM system.tables WHERE database='system'`;
PostgreSQL row counts of the seeded probe tables; health endpoint.

| # | Phase | Pass criteria |
|---|---|---|
| A | 1.23.9 baseline (seeded 500 CH + 500 PG probes, diagnostic copies, pre_recovery evidence) | snapshot recorded |
| B | **Forced failure → rollback** with the candidate (e.g. make a hook after step 10 fail in a lab-signed variant, or stop ClickHouse health during the hook) | updater reports rollback; 1.23.9 code restored; units compare equal to the pre-update baseline (no collector/NCM worker/watchdog unit where the base had none); `systemctl --failed` empty, no restart loops; ClickHouse running/healthy; container survives `docker restart`; all seeded rows, detached count and evidence unchanged; health OK |
| C | Candidate upgrade from 1.23.9 | all steps OK; schema gate clean; mounts `/etc/zenplus/clickhouse/...`; effective config TTL/partition per log; `text_log` warning; legacy `_0` tables present; evidence + unknown tables untouched; seeded rows/detached unchanged; watchdog active, heartbeat < 60 s, `last_error` empty |
| D | Idempotence | re-run `setup-storage.sh` (or re-apply the same package): `changed=0`, no container re-creation, no data change |
| E | Retention | advance the lab clock (or set newest dates) past 3/7 days: legacy copies + disabled logs dropped by the hourly sweep exactly once; evidence/unknown/current tables and monitoring data unchanged |
| F | Live + backlog replay (real agents or a replay harness against the lab) | rows per insert ≫ 1, new parts/minute far below 1.23.9 baseline, no lost/duplicated samples (unique (server_id,timestamp) = rows), backlog drains |
| G | Pressure on a disposable filesystem (lab only) | crossing threshold drops diagnostics first, monitoring data only per policy, reclaimed bytes measured, stops at target, no repeated over-deletion |
| H | Outages | API + PostgreSQL stopped: watchdog keeps sampling and acting; ClickHouse stopped: error recorded, ingest pause only when critical, error cleared only after a successful ClickHouse call |
| I | Upgrade 1.24.1 → candidate (.221 layout) | container re-created with `/etc` mounts; no data change |
| J | Reboot after C and after B | ClickHouse starts; no Docker-created directories at mount sources |

.221 itself: only phase I/C read-only checks after Codex approves; no pressure, power or
disk tests there.
