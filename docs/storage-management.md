# Storage management (Settings → Storage)

## Layout model

The **data volume** is whatever LVM logical volume backs `/data`. Appliances
built with a dedicated data LV use it; single-disk appliances (for example
`.221`) keep `/data` on the root LV `ubuntu-vg/ubuntu-lv`. Nothing assumes LV
names: the API and the root helper resolve the LV with `findmnt --target /data`.
The **storage pool** is the volume group holding that LV.

## Privilege boundary

All disk and volume changes go through the root-owned
`/usr/local/sbin/zenplus-volume-helper` (source `scripts/zenplus-volume-helper`),
installed and granted by `scripts/setup-storage.sh` (runs on every OTA). The
helper re-validates every request against the live system:

| Verb | Effect | Refuses when |
|---|---|---|
| `inventory` | lsblk, LVM reports, partition geometry, `/`, `/data` mounts | — |
| `rescan` | SCSI host scan + per-disk size rescan | — |
| `add-disk DISK [--wipe]` | `pvcreate` + `vgextend` into the data VG | system disk, mounted/held, in another VG, existing data without `--wipe` |
| `extend MOUNT all\|BYTES` | `lvextend` (whole extents) + `resize2fs`/`xfs_growfs` online | mount not `/` or `/data`, not enough free extents |
| `grow-pv PV` | `growpart` (partition PVs) + `pvresize` | PV outside the data VG |
| `evacuate PV` | background `pvmove` | other PVs lack free space for its extents |
| `abort-evacuation` | `pvmove --abort` | no move running |
| `remove-disk PV` | `vgreduce` + `pvremove` | PV still holds extents, last PV |
| `remove-missing` | `vgreduce --removemissing` (never `--force`) | no missing PVs |

Mutating verbs take an exclusive lock and refuse while a `pvmove` runs. Exit
status: `0` done, `2` rejected (nothing changed), `1` failed.
`bin/expand-storage.sh` is the operator CLI over the same helper.

## Operator procedures

- **Add a disk:** attach it in the hypervisor → *Rescan disks* → *Add to pool*,
  choosing all, part, or none of it for the data volume. Disks with partitions
  or filesystems require typing the device path to confirm erasing them.
- **Enlarge a disk:** grow it in the hypervisor (remove VMware snapshots first)
  → *Rescan disks* → *Claim space* (optionally allocating it immediately).
- **Remove a disk:** *Remove* moves its data to other pool disks online
  (requires that much unallocated space elsewhere), then a second *Remove*
  takes it out of the pool. Detach it only once it shows *Unused*. Keeping
  added capacity unallocated is what makes later removal possible: ext4 cannot
  shrink online.
- **Missing disk:** shown as critical when it held data (reattach and rescan);
  an empty missing disk can be forgotten.

## Why appliances filled up

Field assessment of a 498 GiB appliance (10 Oct 2026) and cross-check on `.221`:

- ClickHouse's **own diagnostic logs** dwarfed monitoring data (`.221`: 15.6 GiB of
  `system.*_log` vs 0.1 GiB of monitoring data; field VM: 47 GiB vs 15.6 GiB). The image
  defaults log at `trace` into `system.text_log` and keep every system log forever in
  monthly partitions. Recoveries left `*_pre_recovery_*` copies nothing ever removed.
- **Small-insert amplification**: every host-agent upload became one synchronous INSERT
  (one new part) per metric kind, each preceded by a `DESCRIBE`. Thousands of single-row
  parts per minute made merges rewrite many GiB and inflated `query_log`/`part_log`.
- The old emergency purge only saw the `zenplus` database, ran inside the API (gone when a
  full disk stopped PostgreSQL), and re-checked usage immediately after `DROP PARTITION`,
  whose space returns only after ~10 minutes.

## Bounded ClickHouse diagnostics

`docker/clickhouse/zenplus_logging.xml` (config.d) and `zenplus_profile.xml` (users.d)
are installed by `scripts/install-clickhouse-config.sh` into `/etc/zenplus/clickhouse`
(staged, validated, renamed atomically; outside the code tree so a rollback can never
remove a bind-mount source) and mounted from there by `docker-compose.yml`:

- server log files and `text_log` keep warnings and errors (100 MiB × 3 files);
- every system log enabled by default gets daily partitions and a TTL (3 days for
  text/trace/profile logs, 7 for query/part/metric logs, 30 for error/backup logs —
  `storage_policy.DIAGNOSTIC_LOG_RETENTION_DAYS`, kept in sync by a test);
- processor profiles, materialized-view logs and sampling-profiler stacks are off.

ClickHouse renames a system log table whose definition changed to `<name>_<n>` on its next
write; those legacy copies, and the logs switched off by the profile
(`processors_profile_log`, `query_views_log`), are dropped once older than their log's
retention, or first under pressure. Only known diagnostic logs are ever touched:
`*_pre_recovery_*` copies are recovery evidence and are never deleted automatically, and
unknown or custom `system` tables are left alone. A failed upgrade's rollback runs
`ota-reconcile-storage-rollback.sh` from the payload (see
`docs/storage-1.24.2-migration-acceptance.md`). `setup-storage.sh` recreates the container when the
mounts are missing.

## Free-space watchdog

`zenplus-storage-watchdog.service` (`app/services/storage_watchdog.py`, policy in
`storage_policy.py`) runs every 30 s as the service account and depends only on
ClickHouse and `/var/lib/zenplus-storage`, so it keeps working when the API or
PostgreSQL is down. Per backing filesystem (`/` and `/data` deduplicated):

- **Triggers**: usage ≥ threshold (85%), free space below the minimum (5 GiB), or a growth
  runway under 6 h (15-minute sample) — the last one sheds diagnostics only.
- **Order**: legacy diagnostic copies → oldest diagnostic days (never today) → whole
  diagnostic tables (error/crash/session/backup logs kept) → monitoring data entirely older
  than the minimum keep window, least valuable category first (raw flows … hourly rollups).
  Monitoring data is only deleted when that can reach the target, or from 95% usage.
- **Bounded passes** (≤ 12 actions); bytes scheduled by `DROP PARTITION` count as pending
  for 15 minutes so one episode never cascades into over-deletion. Scheduled bytes and the
  measured change in free space are recorded separately (`watchdog_purge`,
  `watchdog_reclaim`).
- **Backpressure**: when free space is critical and nothing more is eligible, it creates
  `ingest-paused`; host-agent results and APM traces then answer 503 with `Retry-After`
  (agents keep the data in their spools). Ping/service/SNMP results are never paused.
  Ingestion resumes once free space is back above the minimum.
- **Audit**: `audit.jsonl` (bounded, rotated); the API imports it into `storage_events`.
  The API writes the last valid policy to `policy.json`; if the service is not running
  the API runs the same tick under the same lock every 5 minutes.

Monthly-partitioned monitoring tables can only be dropped a month at a time; their TTLs
remain the normal age-based retention. NetFlow (Go collector) ingestion is not paused.

## Host telemetry group commit

`app/services/host_insert_batcher.py`: concurrent uploads join a per-table batch written
as one INSERT after ~1 s or 20k rows (`HOST_INSERT_BATCH_DELAY_S`,
`HOST_INSERT_BATCH_MAX_ROWS`), with column types described once per process. Each upload
is acknowledged only after its batch is written. A single-upload batch keeps the upload's
own deduplication token; a combined batch retries with one stable token, and after an
unknown outcome each upload is acknowledged only if its own rows are stored. A replay of an
upload still being written (HTTP deadline) joins that write instead of inserting again.

## OS logs

`setup-storage.sh` caps the journal (`SystemMaxUse=1G`, `SystemKeepFree=4G`), adds
`maxsize 500M` to the rsyslog logrotate stanza where rsyslog is installed, and runs
logrotate hourly so size ceilings are enforced. Cleanup estimates use allocated bytes
(`du` without `-b`) and only what `apt-get clean` removes.

## Acceptance still required on a real appliance

Unit tests simulate ClickHouse and the filesystem. Before release, on a disposable or
reference appliance: confirm ClickHouse 24.10 starts with the logging config and creates
the TTL'd tables (legacy copies appear as `<name>_0`), replay a backlog plus live agent
traffic and compare parts/merges per minute and sample counts, and fill a test filesystem
through the thresholds with an API/PostgreSQL outage to verify purging, reclamation and
the ingest pause.


### Storage measurement and protected copies (1.24.3)

OS measurements tolerate absent optional cache files while preserving actual
read failures. Unknown sizes are displayed as unavailable; they remain part of
the filesystem usage and Other segment. Rsyslog's size rule is validated with
the complete global logrotate configuration. Its hourly check and archives mean
the ceiling is not a hard upper bound on the whole log directory.

Error, crash, session and backup log copies keep their incident history under
pressure. A legacy copy may still expire normally after its newest day exceeds
retention, but unknown age cannot establish expiry for a protected copy. The
capacity forecast describes a historical footprint estimate; the watchdog's
15-minute filesystem samples are the source for observed growth.


## Commercial storage experience (1.24.5)

The dashboard uses product-level category labels and structured operation summaries.
Disk models, serials and capacities remain visible for physical identification;
filesystem paths, volume-group names, table names and raw service errors are not
rendered in the Storage interface. Administrator API contracts and support evidence
remain available internally; this presentation boundary is not a security boundary.
Disk operations continue through the existing privileged helper with fresh safety
validation. Typing the displayed disk name confirms the selected operation; the
request retains the precise device identifier expected by the helper.

Automatic cleanup uses percentage thresholds plus a capacity-aware reserve. The
configured `min_free_gib` is a ceiling: effective minimum free bytes are the lesser
of that value and 10% of writable capacity. The critical floor is the lesser of the
configured critical value, 4% of writable capacity and the effective minimum.
Writable capacity excludes reserved blocks. Trigger, displayed reserve and resume
hysteresis use the same per-filesystem limits, recalculated after expansion.
Cleanup targets, age limits, protected/evidence exclusions, pending reclamation,
bounded work per pass, separate-volume isolation and critical upload backpressure
are preserved. Protected/recent data and unrelated files can still require an
operator to expand storage; protection is not a guarantee against every disk-full
condition. Availability collection remains active; bulk agent/APM uploads can pause.

Log lifecycle groups old appliance logs by their conservative expiry date. Expiry
is the day after newest data plus retention, matching the hourly legacy sweep's
strict whole-day comparison. Unknown dates are not guessed. Evidence has no
automatic expiry. This reads existing part metadata, not telemetry rows.

The UI stops disk-topology polling on retention, backup and activity tabs, and
shares recent overview measurements across tab navigation. Mutation invalidation
still refreshes affected data. Rendered fixture tests cover all five tabs with
internal names and errors deliberately present in backend-shaped data.
