# System status (Settings → System status)

The tab that was called **Storage** is now **System status**: the health of the
appliance itself (not of the devices it monitors) and its storage on one page.

| Section | Address | Content |
|---|---|---|
| Overview | `/settings/general?tab=storage` | CPU, memory, swap, load, uptime, services, data stores, software, last 24 hours |
| Storage | `…&section=storage` | Capacity, automatic protection, log lifecycle (the old Storage overview) |
| Disks & capacity | `…&section=disks` | unchanged |
| Retention & cleanup | `…&section=retention` | unchanged |
| Backups | `…&section=backups` | unchanged |
| Activity | `…&section=activity` | unchanged |

The tab keeps its address, `?tab=storage`. Role page lists
(`roles.page_access`, keyed by that address in `page_catalog.json`) and the
links in reports that were already sent refer to it, so nothing stored has to
change. `?tab=system-status`, `?tab=system`, `/settings/system-status` and the
old `/settings/storage` open the same tab. The permission is unchanged:
`system.admin`.

Code: `dashboard/src/components/SystemStatusTabContent.tsx`,
`components/system/` (overview) and `components/storage/` (storage sections);
`server/app/services/appliance_health.py`, `server/app/api/v1/system_status.py`.
Storage itself is described in `docs/storage-management.md`.

## What the overview reads

`GET /api/v1/system/status` (administrator only, read-only):

- **CPU**: busy share of the last full minute from `/proc/stat`, with the share
  spent waiting for disk and the share taken by the hypervisor. Load averages
  are shown beside the number of cores.
- **Memory and swap** from `/proc/meminfo`. "Used" is total minus *available*,
  so page cache does not count as used.
- **Uptime** from `/proc/uptime`; **storage** is the filesystem behind `/data`
  (whatever backs it), as `df` reports it.
- **Services**: one `systemctl show` call (no privilege needed) gives state,
  memory, CPU time, time since start and automatic restarts. PostgreSQL is
  found by pattern (`postgresql@*.service`), never by a version number.
  PostgreSQL, ClickHouse and Redis are also **asked directly**, each with a
  4-second deadline: a store whose unit is active but that does not answer is
  shown as down. An optional service that the administrator disabled is
  "Switched off", not a fault; one that is not installed has no row.
- **Data**: PostgreSQL database size, ClickHouse monitoring data and its own
  diagnostic logs (active parts), Redis memory; whether monitoring results are
  being processed (the controller recovery state) and whether agent and
  application uploads are paused for low space.
- **Software and system**: installed ZenPlus version and time, the last update
  and how it ended, processor, clock synchronisation.

**Naming.** The page, the API response and the finding messages name a service
only by what it does: Web application, Monitoring engine, Configuration
database, Metrics database, Cache and live updates, Web server, Storage
protection, Flow collector, Syslog receiver, Configuration backup workers.
Which software is behind a name (the database engines, the web server, the
operating system), versions and unit names are implementation detail and are
not sent or shown; this document and the code are where they are written down.
`scripts/test-storage-render.mjs` and `tests/test_appliance_health.py` fail if
one of those names reaches the page or the response.

A reading that cannot be taken is `null` and is shown as "—" or
"Unavailable". It is never replaced by zero. Driver errors go to the log, not
to the page.

The public `GET /api/v1/system/health` is unchanged: it stays the liveness
check the installer and the updater call.

## Findings at the top of the page

Deliberately few and conservative; the page explains the appliance, it does not
page anyone (`appliance_health.findings`):

| Finding | When |
|---|---|
| critical: a core service is down | web application, monitoring engine, either database, the cache or the web server not running / not answering |
| critical: monitoring interrupted | results are not being processed |
| critical / warning: memory | ≥ 97 % / ≥ 90 % in use |
| warning: swap | ≥ 50 % of swap in use |
| warning: CPU | average ≥ 90 % over the last 15 minutes; without history, 15-minute load ≥ 1.5 per core |
| warning: disk wait | ≥ 25 % of the last 15 minutes |
| warning: optional service down, uploads paused, clock not synchronised | |

Storage findings (critical and warning) from the Storage section are repeated
on the overview in the same words, with a link.

## History

`GET /api/v1/system/status/history?hours=1..24`. One sample a minute (CPU, disk
wait, memory, swap, load, storage use) is kept for 24 hours in a Redis sorted
set (`zenplus:appliance_health:samples:v1`, about 150 KiB). Every API worker
measures CPU over its own minute; the worker that claims the minute writes the
sample, so there is one row per minute whatever the worker count. No migration,
no new service, no ClickHouse table.

- A range of up to 3 hours keeps the minute resolution; longer ranges (6 h,
  24 h) are returned in 5-minute steps with the mean and the busiest minute of
  each step.
- A step nothing was sampled in has `null` readings: the chart breaks the line
  there instead of joining it across an outage.
- Without Redis the page still shows current values and says that history is
  unavailable. After the update that adds the page, history starts empty and
  says how much of the range has been collected.
- The change in storage use over the window is shown under the chart. It
  replaces reading a trend from the watchdog's 15-minute sample, which swings
  with background merges (see the review, L1).

Not kept: per-service history, per-core CPU, network throughput. A server agent
on the appliance itself, or a longer history in ClickHouse, would be the next
step if a week or a month is wanted; see the review for the recommendation.
