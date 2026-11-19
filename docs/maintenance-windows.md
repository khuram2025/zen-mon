# Maintenance windows

A maintenance window is planned downtime. While one is in force the asset keeps
being monitored and its metrics are recorded, but nothing about it is alerted.
Windows live in `device_maintenance` (migrate-053; recurrence and server scope
migrate-207, alerting plan step 2.7, gap G-6).

## Kinds

* **One-off**: `starts_at` to `ends_at`, both ends included. Unchanged by
  migrate-207.
* **Recurring**: repeats weekly. An occurrence starts on each chosen weekday
  (`recur_days`, ISO 1 = Monday to 7 = Sunday) at `recur_start_time` local time
  in `recur_timezone` (default: the organisation's time zone, Settings →
  General) and lasts `recur_duration_minutes` (1 minute to 7 days).
  `recur_until` (optional) is the last local date an occurrence may start.
  Example: "every day at 02:00 for 1 h (Asia/Riyadh)" covers the nightly
  backups that used to page as P1.

Rules for recurring windows:

* An occurrence that runs past midnight belongs to the day it starts: a
  Saturday 23:00 window of 3 hours covers Sunday 00:00-02:00 even when Sunday
  is not chosen.
* The duration is absolute minutes, so a DST change never stretches or
  shortens an occurrence. A local start time that does not exist on the day
  the clocks go forward (02:30 in Europe/Berlin on the last Sunday of March)
  starts at the same instant as it would have with the old offset (01:30 UTC);
  a start time that happens twice when the clocks go back starts at the second
  of the two (the later instant). This is how PostgreSQL reads such times.
* For a recurring window `starts_at`/`ends_at` hold its **first occurrence**
  (the API writes it: the first occurrence that has not ended when the window
  is created, so a window created during an occurrence is in force at once).
  Earlier occurrences never count. Software that does not know about
  recurrence (an older release after a rollback) therefore sees only the first
  occurrence, never an endless window.

## Scope

| `scope_type` | Covers |
| --- | --- |
| `device` | one device (`scope_device_id`) |
| `group` | the devices of a group and its subgroups (`device_group_member_closure`) |
| `tag` | devices carrying the tag (`devices.tags`) |
| `all` | every device |
| `server` | one server (`scope_server_id`, deleted with the server) |
| `server_group` | the servers of a group (one whose `asset_kinds` include servers) and its subgroups |
| `server_tag` | servers carrying the tag (`servers.tags`) |

Device scopes never cover servers, and server scopes never cover devices, so
an existing tag or group window did not start silencing servers. A server is
also in maintenance while **the device it is linked to** (`servers.device_id`)
is: they are the same machine.

## One definition of "in force"

`device_maintenance_occurrences(m, from, to)` (SQL) returns a window's
occurrences that overlap `[from, to]`; for a one-off window that is the window
itself. `device_maintenance_active(m, at)` wraps it. Everything that asks
whether a window is in force uses these functions:

* the Go poller (`LoadActiveMaintenanceDeviceIDs`; falls back to the old
  one-off statement while the function does not exist yet);
* `app.services.maintenance_windows`: `device_in_maintenance`,
  `server_in_maintenance`, `active_device_ids`, `active_server_ids`
  (`alert_engine._device_in_maintenance` delegates to it);
* the sensor API (occurrences in the freshness range, per sample);
* uptime, SLA and report queries (one interval per occurrence).

`app.services.maintenance_windows` mirrors the math in Python for the API (the
first occurrence it stores and the current or next occurrence it shows);
`server/tests/test_maintenance_windows_pg.py` checks that SQL and Python agree,
across DST changes in Europe/Berlin and America/New_York.

## What honours a window

| Evaluator | Asset | Effect |
| --- | --- | --- |
| Poller / sensor status machine | device | status shows Maintenance; transitions and alerts are suppressed |
| Device status, traps, syslog, flap notices (`alert_engine`, `network_events`) | device | nothing raised |
| SNMP metric rules (`network_alert_service`) | device | not evaluated |
| Monitoring gaps (`monitoring_gap_alert_service`) | device (`snmp_unreachable`), server (`agent_no_metrics`) | skipped; an open incident stays as it is |
| Server availability (`server_availability`, Device Down / Agent offline, the sweep's built-in agent-offline alert) | server | state `maintenance`: nobody is paged |
| Service metric and virtualisation rules | the linked device | skipped |
| NetFlow alerts | the exporter device | suppressed |
| Device Tracker (`udt_alert_service`) | the reporting switch | events are consumed without an alert; port capacity is not judged |
| Server metric, service and process rules (`host_alert_service`: CPU, memory, swap, filesystem, disk, network, clock, services, processes) | server | not evaluated; an open incident stays open, unresolved and quiet until the window ends |
| Server restart / shutdown rules (`host_alert_service`) | server | events in the window are marked `maintenance_suppressed` and never announced |

Not covered, because the alert has no device or server: APM and RUM rules
(per APM service or browser application) and NetPath rules (per probe; a probe
targets a host name or address, not an inventoried device).

`host_alert_service.evaluate_host_rules` reads
`maintenance_windows.active_server_ids` once per pass and leaves those servers
out of every rule except availability rules, which honour server windows
through `server_availability.load_signals` (state `maintenance`). The
restart/shutdown suppression uses `s.ping == "maintenance" or s.maintenance`.

## When a window ends

The same for one-off windows and each occurrence of a recurring window:

* The poller and a sensor stop treating the device as in maintenance and apply
  its confirmed status, with alerts (`docs/device-status-policy.md`).
* Evaluators that skipped the asset judge it again on their next pass; an
  incident that stayed open during the window resolves or continues normally.
* A ping-disabled device that was put in Maintenance when a window was created
  in force is set back to Unknown by the staleness sweep once no window covers
  it.

Creating a window that is in force now sets its devices to Maintenance at
once and resolves their open device alerts (as before). Server windows do not
resolve open server alerts.

## API

`POST /api/v1/device-maintenance` (`devices.manage`; a server scope also needs
`servers.manage`, and its server must be visible to the caller):

```json
{"scope_type": "server", "scope_server_id": "…", "reason": "Nightly backup",
 "recurrence": {"days": [1,2,3,4,5,6,7], "start_time": "02:00",
                "duration_minutes": 60, "timezone": "Asia/Riyadh", "until": null}}
```

A one-off window sends `starts_at` and `ends_at` instead of `recurrence`. For a
recurring window `starts_at` (optional) is the moment from which occurrences
count; `ends_at` is refused. Unknown time zones, days outside 1-7, durations
outside 1 minute to 7 days and a recurrence that never starts are refused with
422. Responses carry `recurring`, `recurrence`, `schedule` (a sentence such as
"Every day at 02:00 for 1 h (Asia/Riyadh)"), `active`, and `next_starts_at` /
`next_ends_at` (the occurrence in force, else the next one).

`GET /api/v1/devices/{id}/maintenance` and
`GET /api/v1/device-maintenance/servers/{id}` list active and upcoming windows
for one asset (a server's list includes its linked device's windows).
`DELETE /api/v1/device-maintenance/{id}` removes one. Create and delete are
audited (`device_maintenance`, with the recurrence).

The device and server pages share one dialog
(`dashboard/src/components/maintenance/MaintenanceDialog.tsx`, model tested by
`dashboard/scripts/test-maintenance-windows.mjs`): one-off or recurring, days,
local start time, duration, time zone, optional end date, and the next
occurrence of each window.

## Known limits

* A ping-disabled device shows status Maintenance only when a window is
  created in force; a later occurrence (or a one-off window that starts later)
  suppresses its alerts without changing the status shown.
* Windows are created and removed, not edited.
