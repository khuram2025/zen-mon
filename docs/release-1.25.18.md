# ZenPlus 1.25.18

System status: the health of the appliance itself, and fixes to storage.

- Settings -> Storage is now Settings -> System status. It opens on an
  overview of the appliance itself: CPU, memory, swap, load, storage use and
  uptime; the state of each service with its running time, memory, CPU time
  and restarts; the size of each data store; whether monitoring results are
  being processed; the installed version and the last update; and the last 1,
  6 or 24 hours as a chart. A reading that cannot be taken is shown as
  unavailable, never as zero. History starts after the update and fills in
  over the following 24 hours.
- The storage sections (Storage, Disks & capacity, Retention & cleanup,
  Backups, Activity) follow on the same tab. Old links and bookmarks still
  open it; the permission is unchanged (full administration).
- Fixed: "Free up space now" could remove all of a table instead of the data
  older than the chosen age (network capture records).
- Fixed: "Claim space" with "allocate right away" allocated the whole
  unallocated reserve instead of the space just claimed.
- Fixed: agent and application uploads could stay paused after a low-space
  episode although free space had returned.
- Fixed: automatic cleanup could stop freeing space when one removal was
  refused; it now continues with the next candidates and reports the refusal.
- Fixed: a disk action could be requested for the operating-system disk; the
  disk helper refuses it. Extending a volume checks the filesystem first.
- Backups: one backup or restore runs at a time; a failed scheduled backup is
  no longer retried all hour and leaves no files; "Keep last N" keeps N; a
  restore interrupted by a restart no longer blocks Restore; "Restart
  services" is asked for only until the services have restarted; an imported
  archive never overwrites a backup the appliance holds; downloads are
  recorded in the audit log and can be imported again.
- Retention is saved even when monitoring history is temporarily unavailable
  and is applied when it is back.
- Storage wording: a purge whose removals failed says so; missing readings
  show as unavailable; short-term growth is no longer shown as a
  remaining-hours estimate; scheduled log cleanup is no longer titled a
  low-space purge; confirmations before forgetting a disk, aborting a data
  move and clearing logs or the update cache.

No migration. Includes 1.25.17. Appliance-only package; minimum upgrade base
1.24.36; signed Windows agent catalog preserved. Details:
docs/system-status.md, docs/system-status-review-2026-11-19.md (what is fixed
and what is still open, with recommendations).
