# Virtualisation alerts

The virtualisation connectors (vCenter, Nutanix Prism, Hyper-V) collect host
connection state, datastore usage and the platform's own alarms on every poll,
but until now none of it could raise an alert. Three rule metrics now read the
connector's latest snapshot (`virtualization_connectors.snapshot`). They are
evaluated by `app.services.virtualization_alert_service` every 60 seconds
(migration 201).

| Metric | Reading | Dimension | Notes |
|---|---|---|---|
| `vm_host_disconnected` | 1 while vCenter reports an ESXi host `disconnected` or `notResponding`, 0 while it reports `connected` | host | vSphere only: Prism and Hyper-V report no connection state. A host vCenter has in maintenance mode is not evaluated. |
| `vm_datastore_pct` | Used % of a datastore (Nutanix: storage container) | datastore | |
| `vm_alarm` | 2 = red / critical, 1 = yellow / warning, 0 = cleared or another colour | alarm | vCenter triggered alarms and Prism alerts. An alarm that is no longer in a current snapshot has cleared. |

The rule wizard offers them under **Virtualisation** (scope: every source,
one source, the source linked to a device, or a source tag; the alarm rule
picks red alarms or red and yellow ones). Each rule takes one condition:

- `vm_host_disconnected`: `== 1` (or `>= 1`);
- `vm_alarm`: `>= 1` for red and yellow alarms, `>= 2` for red only;
- `vm_datastore_pct`: `>`, `>=`, `<` or `<=` with a limit from 0 to 100. An
  optional `reset_threshold` sets the clear level and must lie on the healthy
  side of the limit.

## Scope

The asset is the virtualisation source (the connector). A rule can be narrowed
to:

- one source: `target` = the connector id;
- the source's **linked device** (`device_id`): the monitored device whose IP
  address or hostname is the connector's endpoint host (an address match wins);
- a source tag (`scope_tag`).

Device group, type, location, template, interface role and service-check scope
are refused. A tag-scoped user may change a rule for one source whose tags they
can see, or a rule scoped to one of their tags.

When the linked device is in maintenance (manual or a device maintenance
window), the whole source is not evaluated and its open incidents stay as they
are. The alert row carries the linked device (`alerts.device_id`), so the alert
shows on that device and storms/grouping can key on it. A source without a
linked device raises alerts without a device.

## Readings and missing data

A snapshot is current while it is no older than two poll intervals plus 30
seconds (the connector page shows it as stale after that) and the connector is
enabled. Readings change only as often as the connector polls (default
5 minutes).

A missing reading never resolves an incident and never counts toward a hold:

- the snapshot is stale (the connector stopped polling or the source is
  unreachable);
- the connector is disabled;
- the host or datastore is no longer in the inventory (its open incident stays
  open until an operator resolves it).

An alarm is different: vCenter removes a cleared alarm from the triggered
alarms, so an alarm with an open incident that is missing from a **current**
snapshot reads 0 (cleared). It never clears from a stale snapshot.

## Lifecycle

The lifecycle matches the other metric incidents
([alert-notification-policy.md](alert-notification-policy.md)):

1. One incident per rule + source + dimension (host, datastore or alarm).
2. The condition must hold for `min_duration` before the incident opens and the
   trigger is sent.
3. The incident clears at the explicit `reset_threshold`. Without one, a
   datastore rule clears **2 points** on the healthy side (`> 85` clears at 83 %
   or less); the host and alarm metrics clear when the state clears.
4. The cleared condition must hold for `clear_duration` (NULL = 300 s, 0 = at
   once) before the incident resolves.

The breach and clear start times are stored in `virtualization_alert_state`. Each
pass takes the `alert_worker_lock` (`zenplus:virtualization_alert_pass`). Every
send goes through `dispatch_to_channels` with the alert id and the delivery
event (`trigger` / `recovery`) and the event family `virtualization`. Cooldown,
schedules, late sends after quiet hours, priorities, escalation and P3/P4
grouping work as for other rules. A recovery is sent only if the trigger was
sent and the rule has `recovery_alert` set.

A disconnected host usually also raises vCenter's own "Host connection and power
state" alarm. A `vm_alarm` rule and a `vm_host_disconnected` rule on the same
source therefore raise two incidents for one event; use one or the other.

## Wording

- "Datastore DS-01 on vCenter vc-01 is 92% full (limit 85%)."
- "ESXi host esx-04 is disconnected from vCenter vc-01." / "... is not responding to vCenter vc-01."
- "vCenter vc-01 raised the red alarm "Host CPU usage" on ESXi host esx-04."

Recovery messages:

- "Datastore DS-01 on vCenter vc-01 is back to 78% full (limit 85%)."
- "ESXi host esx-04 is connected to vCenter vc-01 again."
- "The alarm "Host CPU usage" on ESXi host esx-04 cleared in vCenter vc-01."

The source is named "vCenter <name>", "Prism Central <name>", "Prism Element
<name>" or "Hyper-V <name>", or just the connector name when it already says
what it is. A datastore reading within 5 points of the limit keeps one decimal.
Custom templates can use `{event_sentence}`, `{asset}`, `{source}`,
`{resource}`, `{alarm}`, `{reading}`, `{threshold}` and the usual placeholders.

## Recommended defaults (not seeded)

- Datastore usage: `vm_datastore_pct > 85`, warning, P3; `> 95`, critical, P2.
- Host disconnected: `vm_host_disconnected == 1`, `min_duration` 300 s, critical.
- Platform alarms: `vm_alarm >= 2` (red), critical, where vCenter's own alarm
  definitions are maintained.
