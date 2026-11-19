# Storage review and System status page (2026-11-19)

Scope: everything the Settings → Storage tab showed and did, the code under it
(API, storage service, watchdog and policy, volume helper, host insert
batcher), and the tab's change into **System status** (`docs/system-status.md`).

Method: three independent code reviews (dashboard, API and service, watchdog
and helper), each finding checked in the code before it was fixed, plus a
read-only look at two appliances on 1.25.17 through a signed-in administrator
session (GET requests only): the development appliance, and a customer
production appliance where nothing was pressed or saved.

State of each finding: **Fixed** (in this change, with a test), **Partly**,
**Open** (with the recommendation). Nothing here needs a migration.

## The worst ones

| # | Finding | State |
|---|---|---|
| S1 | **"Free up space now" could delete a whole table, today's data included.** The purge listed partitions with `max(max_time) < cutoff`. A table without a DateTime partition key reports 1970 there: the unpartitioned host network-capture table (category NetFlow records), and any table partitioned on a Date column. Its only partition was "older than" any cutoff. The preview showed it with 1970 dates | **Fixed**: the newest data of a partition is taken from `max_time` or `max_date`; a partition that cannot be dated is never listed. Row-precise mode still reaches such tables |
| U1 | **"Claim space" with "allocate right away" took the whole unallocated reserve**, not the space just claimed. The reserve is what makes removing a disk possible later; ext4 cannot shrink | **Fixed**: only the claimed bytes are allocated |
| W3 | **Agent and application uploads could stay paused for ever.** The pause starts at critical free space and was lifted only when the cleanup state was back to "ok" (below the 85 % threshold). A volume that settled at, say, 90 % with nothing left to purge kept answering 503 with tens of GiB free | **Fixed**: uploads resume when free space is above the minimum and usage is 2 points under the critical percentage |
| W1 | **One deletion ClickHouse refuses blocked all cleanup.** The same first candidate was planned every 30 s, "covered" the need, and later candidates were never tried; no error was reported and an audit record was written every tick | **Fixed**: a refused removal is skipped for 30 minutes, the next candidates run in the same pass, and the refusal is reported as a protection error. **Open**: sending `max_table_size_to_drop = 0` with the watchdog's drops (ClickHouse refuses drops over 50 GB by default; a customer appliance once held 41 GiB of logs in one table) needs a check on a real ClickHouse 24.10 first |
| W4 | **The volume helper accepted "move data off" and "remove" for the operating-system disk.** Only the page said it cannot be removed; the API passes any PV of the data volume group through | **Fixed** in the helper (`system_disk`) |
| U3 / S9 | **A purge whose deletions all failed said "Purge scheduled — 0 B"**; with ClickHouse down the purge answered a bare 500 | **Fixed**: failures are shown; 503 "nothing was removed" when history is unavailable |

## Seen on the appliances (read-only)

| # | Finding | State |
|---|---|---|
| L1 | Overview said "Recent growth 1.1 GiB/hour … remaining capacity 301 hours", then 1.6 and 2.0 GiB/hour (174 hours) within three minutes, on a volume 26 % used whose own forecast on the same page was 2.4 GiB/day. The figure is the watchdog's 15-minute sample, which swings with background merges | **Fixed**: named "growth over the last 15 minutes"; a remaining-time estimate is printed only under 72 hours. The System status overview shows the measured change in storage use over 24 hours |
| L2 | Activity titled the scheduled removal of expired logs "Low-space purge" on an appliance that was never low on space | **Fixed**: "Log cleanup" unless a low-space trigger started it |
| L3 | Log lifecycle listed two rows both named "Current appliance logs" (7-day and 3-day retention) | **Fixed**: named by what they are |
| L4 | 46 of the 50 Activity rows were 41-day-old automatic purges of a few KiB; each said the amount twice ("freed 1.1 KiB · 1.1 KiB reclaimed") | **Partly**: the duplicate is gone. **Open**: paging, and not recording purges that free nothing worth a line |
| L5 | Capacity figures match `df` on the development appliance (total, used, available, reserve), and the category breakdown adds up to "used" on the customer appliance | No defect |
| L6 | `GET /api/v1/system/health` is public and returns the version and the state of each service | **Open** (not changed here: the installer and the updater call it). Recommendation: answer `{"status": …}` only when the caller is not signed in or not local |

## Dashboard

| # | Finding | State |
|---|---|---|
| U2 | Add disk with a custom amount at the maximum: the disk was erased and joined, then the allocation was refused (a PV is slightly smaller than its disk) and the page said "Add disk failed" | **Fixed**: the maximum asks for all; a disk that joined but was not allocated is reported as that |
| U4 | With the metrics database down every category showed "0 B" | **Fixed**: "Unavailable" |
| U5 / S5 | A backup could be deleted while it was being restored; two restores, or a restore during a backup, could run together; two API workers could both start a backup | **Fixed**: one backup or restore at a time (409), decided under an advisory lock |
| U6 / S7 | "A restore has completed. Restart services" never went away; a restore interrupted by a restart stayed "running" for ever, which disabled Restore | **Fixed**: the banner follows `restore_needs_restart` (a restore finished after these services started); stale restores are failed by the sweep |
| U7 | A retention save that failed for some tables pointed to an Activity entry that read as success | **Fixed** |
| U8 | Fractional percentages or days passed the form and ended in a generic 422 | **Fixed** |
| U9 | The server's reason was discarded (invalid archive, missing files); a rescan never said what it found; two identical "running low" rows when `/` and `/data` are separate | **Fixed**. **Open**: the reason a disk cannot be removed is still fixed text |
| U10 | No confirmation on Forget disk, Abort data move, Clear older service logs, Clear update cache | **Fixed** |
| U11 | Import had no progress and could be started twice; Create stayed enabled while a backup ran | **Fixed**. **Open**: the download is still buffered in the browser (see S13) |
| U12 | Purge and system cleanup timed out in the browser after 30 s | **Fixed** (10 minutes) |
| U13 | Missing readings shown as "0 B", "0.0%", "0 disks · Fully allocated" | **Fixed**: "—" / "could not be read" |
| U14 | After a reload during a long add or extend nothing shows that it is still running (only a data move is exposed) | **Open**: expose the helper's lock in the layout |
| U15–U19 | Schedule hint used year-2000 clock offsets; "about 0 days" / "1 days"; an invalid accent colour; unlabelled switch and radios | **Fixed** |

## API and storage service

| # | Finding | State |
|---|---|---|
| S4 | A failing scheduled backup was started again on every sweep of its hour (up to 12), and each left its partial files behind | **Fixed**: one attempt per window; a failed backup's files are removed |
| S11 | "Keep last N" kept N + 1 (known from the 2026-11-18 review) | **Fixed**: pruning runs on every sweep, after the new backup has completed |
| S10 | Saving retention failed entirely while the metrics database was down (known), and the watchdog had already been handed the policy the page said was not saved | **Fixed**: the policy is saved first; history retention is applied by the sweep when the database answers |
| S3 | A downloaded full backup could not be imported again: `--transform` renamed both trees. The test built its archive by hand and did not see it | **Fixed**; the test now runs the real `tar` command |
| S13 | An upload with the id of a backup the appliance holds overwrote it, even one being taken; an unknown manifest kind gave 500 | **Fixed** (409). **Open**: three copies of the upload on disk, no size cap or free-space check; nginx's 1 MB body limit on the installer path (known) |
| S14 | Downloading a backup (database dump and configuration secrets) left no audit entry and worked on a failed or running backup | **Fixed**. **Open**: `tar`'s exit status is not checked |
| S2 | The API account cannot read ClickHouse's snapshot directories (owner uid 101, mode 0750): deleting a full backup says "deleted" and leaves the snapshot; its size is not counted; a download silently omits it | **Open**: needs helper verbs (size, delete, archive by backup id) or a default ACL from `setup-storage.sh`. Until then full backups are the weak part of this page |
| S6 | Restoring metrics data drops each table before restoring it; a failed restore leaves that table empty | **Open**: restore to a temporary name and exchange |
| S8 | A PostgreSQL restore rewinds the backup catalogue to the dump's (later backups lose their rows, the restore's own events vanish); the backup's version is not compared with the appliance's | **Open**: re-insert the catalogue after the restore; refuse a version mismatch |
| S12 | A grow that fails half-way records nothing for the PVs already claimed | **Open** (see W2) |
| S15 | Configuration restore writes `.env` last and not atomically; on an appliance where the service account cannot write it, the databases are already replaced when it fails | **Open**: check writability in the preflight |
| S16 | `human_size` labels binary units "GB" (reports quote it); a retention below a category's minimum is raised silently | **Open** (cosmetic; the page itself prints GiB) |
| S17 | Directory sizes are measured on the event loop; the 600 s helper timeout stops `sudo`, not the dump | **Open** |

## Watchdog, policy, volume helper, batcher

| # | Finding | State |
|---|---|---|
| W2 | `extend`: when `lvextend` succeeded and the filesystem did not grow, the helper said only "resize2fs failed" and a retry said "no unallocated space"; the filesystem type was checked after the volume had changed | **Partly**: the type is checked first (nothing changes), and the failure says the volume was extended and the filesystem was not. **Open**: a helper verb that grows only the filesystem, offered when the volume is larger than its filesystem |
| W8 | A filesystem that cannot be measured counted as healthy | **Partly**: no reading at all is now an error. **Open**: one missing mount of two |
| W9 | A `policy.json` or pause marker that is not a JSON object raised on every tick / every upload | **Fixed** |
| W5 | While ClickHouse is down the batcher holds shared worker threads for its 12 s retry window | **Open**: its own bounded executor and an early 503; needs a load test |
| W6 | Deleted bytes count as "pending" for a fixed 15 minutes even after they are back | **Open**: cap by what is still inactive in `system.parts` |
| W7 | The audit import reads the last 200 lines; more than that between imports never reaches Activity | **Open**: import from the last imported id |
| W9b | `setup-storage.sh` still grants `zenplus-lvm-helper pvresize *` (no caller left); a stale pause marker is trusted without the watchdog's heartbeat; the helper leaves an orphan PV when `pvremove` fails after `vgreduce`; legacy copies of logs with a digit in their name are not classified | **Open** |

## From the 2026-11-18 review

"Keep last N keeps N + 1" and "a retention save fails entirely while ClickHouse
is down" are fixed here. Still open: nginx's body-size limit for backup
uploads, and restoring the shipped per-table TTLs when a retention field is
cleared.

## What was verified where

- **On an appliance, read-only**: the Storage tab's five sections and their
  API responses on both appliances; capacity figures against `df` on the
  development appliance; the findings marked L above. The new health collector
  was run on the development host against its real `/proc` and systemd (the
  databases were not asked): 16 cores, memory, load, storage 61 %, ten services
  with memory, CPU time and restarts, the configuration database's unit found by
  pattern.
- **In tests only**: every fix above (fakes for ClickHouse, PostgreSQL, the
  filesystem and LVM), the System status API and its sampler, the rendered page
  in both themes with synthetic data. The page has not run on an appliance:
  that needs a release. Disk operations were not run on real disks.
- **To check on the canary**: the overview against `top`/`free`/`systemctl`
  while it runs, history filling in after the update, a service stopped on
  purpose (for example the flow collector) showing as down, and the upload
  pause clearing on a test filesystem between 85 % and 97 %.
