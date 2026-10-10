# ZenPlus 1.24.1 — Storage that cannot fill the disk, and a rebuilt Storage page

Appliances were filling their disks with ClickHouse's own diagnostic logs, not
with monitoring data: on one appliance 47 GiB of internal logs against 15.6 GiB
of monitoring data, on another 15.6 GiB against 0.1 GiB. This release bounds
those logs, writes agent telemetry far more efficiently, and adds an
independent watchdog that keeps free space available even when the database is
down. Appliance-only: separately installed Windows agents are untouched.

- **Bounded ClickHouse diagnostics.** ClickHouse now logs warnings and errors
  only, keeps its log files to 3 × 100 MiB, and every internal log table is
  partitioned by day and expires after 3, 7 or 30 days. Old copies of these
  tables left by upgrades, configuration changes or recoveries are removed once
  they expire. ClickHouse restarts once during the upgrade to apply this.
- **Free-space watchdog.** A new service checks free space every 30 seconds,
  independently of the web application and PostgreSQL. When usage passes the
  threshold (85 % by default), free space drops below the minimum (5 GiB), or the
  disk is projected to fill within six hours, it frees space in a fixed order:
  leftover diagnostic copies, the oldest diagnostic log days, whole diagnostic
  logs, and only then the least valuable monitoring data older than the minimum
  keep window. Monitoring data is only deleted when that can actually reach the
  target, or from 95 % usage. If nothing more can be freed, host-agent and APM
  uploads pause (agents keep the data and resend it) before the disk fills;
  ping, service and SNMP monitoring never pause.
- **Efficient agent telemetry.** Uploads arriving together are written as one
  insert per table instead of one tiny insert per metric kind, which removes
  most of the merge churn and the logs it produced. An upload is acknowledged
  only after its data is stored.
- **Operating-system logs capped.** The system journal is limited to 1 GiB (4 GiB
  kept free) and syslog files rotate at 500 MiB.
- **Storage page rebuilt** (Settings → Storage), in five sections:
  - *Overview*: actionable findings, what fills each filesystem (monitoring data,
    ClickHouse diagnostic logs, merged parts awaiting removal, detached parts,
    PostgreSQL, logs, package cache, backups and unclassified space), used,
    available and reserved space, growth rate and time until full, the
    fastest-growing tables, the watchdog's state, and every diagnostic log table
    with its retention.
  - *Disks & volumes*: every disk with its role; add a new disk, claim space after
    enlarging a virtual disk, allocate all or part of it, and remove a disk
    safely (its data is moved off first). Works whether `/data` has its own
    volume or shares the system volume. Missing disks are detected.
  - *Retention & cleanup*, *Backups* and *Activity* (watchdog actions with scheduled
    and measured space; repeated entries collapsed).
- **Fixes.** Disk expansion failed on appliances where `/data` is on the system
  volume; enlarged virtual disks could not be used; the old emergency purge
  could delete monitoring history without relieving a disk filled by other
  files and logged an entry every five minutes; cleanup estimates overstated
  the journal and package cache; dialogs hid their last line behind the
  buttons.

**After the upgrade.** Existing internal log tables are kept as legacy copies
and removed within 3–7 days, or immediately if the disk is under pressure. The
Storage page shows them while they remain.

No database migrations. Appliance-only, minimum prerequisite 1.23.9. Details:
`docs/storage-management.md`.
