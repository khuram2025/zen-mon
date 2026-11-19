# Parent/child dependencies

A dependency says that a device or a server sits behind a parent device: an
access switch behind its distribution switch, a server behind the switch it is
cabled to. While the parent is down, the child's own outage is a consequence,
not a second problem, so ZenPlus records the child's alert as held instead of
paging for it. One outage sends one alert.

Alerting audit 2026-11-19, gap G-13. Table `topology_dependencies`
(migrate-014, extended by migrate-203).

## What a dependency does

A row is honoured when it is **enabled** and **suppresses alerts**. Its parent
counts as unhealthy while the parent device's status is **down, degraded or
unknown** (worst first). Then:

| Path | Effect on the child |
|---|---|
| Device status (Device Down and other status rules) | The down transition is written as a resolved alert with `suppressed_by_dependency`, never sent (`alert_engine._find_suppressing_dependency`). |
| Service checks linked to the child device | Same, for the check's down alert. |
| Traps from the child | Not raised. |
| SNMP metric rules (`network_alert_service`) | Missing data under a failed parent is marked, never treated as a breach. |
| Server availability (`server_availability`) | A device → server row: when the server's agent is silent, ping cannot vouch for it and the parent is unhealthy, the state is `upstream_down`. Neither the Device Down / Agent offline rules nor the built-in *agent offline* alert page for it; an incident already open stays open. A server that answers ping is not cut off by its parent (`agent_offline` as before). |
| Server metric, service and process rules (`host_alert_service`) | A device → server row: a new incident while the parent is unhealthy is recorded with `suppressed_by_dependency`, the dependency and parent (`dependency_id`, `parent_device_id`, `parent_hostname`, `parent_status`, `dependency_type`) and `notified: false`; nobody is paged. If the server still breaches once the parent is healthy, the trigger is sent then (like a quiet-hours trigger); if it clears first, it resolves without a recovery notice. An incident already open (and paged) before the parent failed stays as it is. Restart and shutdown events are the server's own and are not suppressed. |

A server behind a down switch usually stops sending metrics, and missing
samples never resolve or raise those rules; the suppression matters for a
server that still reports (a degraded parent, a second path).

## Who owns a row

`topology_dependencies.source` (migrate-203):

| source | Written by | Removed by |
|---|---|---|
| `manual` | The Dependencies page / API | The Dependencies page / API |
| `topology` | Accepting a topology suggestion | The Dependencies page / API |
| `discovery_rule` | A discovery rule's *Depends on* action | The rule (undo, un-match, enforce); never the API |
| `controller` | The controller sync for FortiLink/FortiAP children | The controller sync; never the API |

The migration backfilled existing rows from what wrote them (the controller
sync's `controller` type and note, a discovery rule's assignment or note;
anything else `manual`). A discovery rule's undo removes only rows with
`source = 'discovery_rule'`, so an operator's row for the same pair survives.

## API (`/api/v1/topology-dependencies`)

* `GET ""` (`devices.view`): every dependency whose parent and child are in
  the caller's scope, with names, addresses, status, `source`,
  `source_label` and `removable`. Filters `device_id` (either end) and
  `server_id` (child).
* `POST ""` (`devices.manage`, both ends in scope): `parent_device_id` and
  exactly one of `child_device_id` / `child_server_id`; `dependency_type`
  (uplink, wan, power, site, service, manual; default uplink), `notes`,
  `source` (`manual` or `topology`). Refused: an existing row for the pair
  (409), a loop — the parent already sits behind the child, directly or
  further down (409), a device depending on itself (409). Audit
  `topology_dependency.create`.
* `DELETE "/{id}"` (`devices.manage`, both ends in scope): `manual` and
  `topology` rows only; engine rows answer 409 with where to change them.
  Audit `topology_dependency.delete`.
* `GET "/suggestions"` (`devices.view`), optional `root_device_id`.

Writes take the `zenplus:topology-dependencies` advisory lock so two loop
checks cannot race.

## Suggestions from the topology

`topology_dependencies.suggest` uses current LLDP/CDP evidence only
(`map_topology.build_adjacencies`: seen within 24 h, both ports present and
up — the network maps' rule). Stale links never become suggestions.

Each connected part of the topology gets a root, the device nearest this
appliance:

1. the device the operator picked as *Start from*;
2. else the device whose address is the appliance's IPv4 default gateway
   (`/proc/net/route`);
3. else the device with the lowest ping round-trip time (`devices.last_rtt_ms`).

A part with none of these gets no suggestions (the page says how many). From
the root, every other device's parent is its neighbour one hop closer. A
device with two or more such neighbours has redundant uplinks — one parent
going down does not cut it off — so it is listed as *not suggested* instead.
Pairs that already have a dependency in either direction, controller-managed
children and devices outside the caller's scope are left out.

Nothing is created automatically: the operator accepts a suggestion, which
POSTs a `topology` row. Devices polled from a remote sensor are rooted at this
appliance like the rest; for a remote site, pick the site's edge device as
*Start from*.

## UI

Network → Dependencies (`/dependencies`, `devices.view`): the list grouped by
parent with owner badges and remove for operator rows (`devices.manage`), an
Add dialog (parent device, child device or server, type, note), and the
*Suggested from topology* tab with Accept. Model:
`dashboard/src/pages/dependencies/dependenciesModel.ts`, checked by
`dashboard/scripts/test-dependencies-ui.mjs`. The device page's Related panel
keeps showing upstream and downstream devices.
