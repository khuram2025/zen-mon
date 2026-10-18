# Device detail page

The device page answers three questions in order: is this device healthy, what
is wrong right now, and where do I go next.

## Layout

1. **Header** (sticky, compacts on scroll): type, name, the **confirmed status**
   (Up, Degraded or Down from the confirmation machine) beside a separate
   **health rollup** ("3 issues"), the identity strip (IP, model, OS, serial,
   uptime, site, group) and one action bar: *Check now* (live ping and SNMP
   tests), *Diagnostics* (one test), *Maintenance*, *Acknowledge* (only when
   alerts are open), and a menu with configuration backup and history, the
   web UI, a copy link, edit and delete. "How status is decided" opens the
   status explanation.
2. **Issues bar**: open problems, worst first and then oldest. When nothing is
   open it collapses to "No open issues · last status change …".
3. **Tabs** with the time range (hidden on Configuration) and an "Updated …"
   marker.
4. **Tab content**. A tab requests its own data only while it is open.

| Tab | Purpose |
|---|---|
| Summary | KPIs (checks answered, latency, CPU, memory, a vendor highlight such as sessions or PoE, interfaces), latency and loss against the Degraded limits with the check strip, recent events, WAN and busiest interfaces, health groups with values, related devices, NetFlow. |
| Performance | Synced charts (CPU, memory, latency, loss, sessions, temperature) with thresholds and min/avg/p95/max; compare with the previous period; CSV and PNG export. A reading opened from another tab is pinned at the top. |
| Interfaces | The interface table inside the device shell (`/devices/:id/interfaces` redirects here). |
| *Vendor* metrics | The template's groups that no declared vendor tab and not Hardware takes. Left out once the declared tabs cover every group. |
| *Template tabs* | Tabs the template declares, e.g. *VPN & SD-WAN*, *Wireless*, *Pools & virtual servers* (see below). |
| Hardware | Template hardware groups (sensors, PSU, fans, temperature, stack, PoE, memory pools, disks, optics) and the ENTITY-MIB component inventory. |
| Events | One range-bound timeline of alerts, status changes, flapping, traps, reboots and maintenance; repeated alerts are grouped (see below). |
| Configuration | Identity and monitoring, owner and notes, monitoring sites, the status policy, configuration backups and connected endpoints. |

Old links keep working: `?tab=overview` opens Summary and `?tab=inventory`
opens Configuration.

## Status and health are separate

The confirmed status is never changed by anything on this page (see
`docs/device-status-policy.md`). The health rollup is computed by
`GET /devices/{id}/summary` from:

* the confirmed status when it is Down or Degraded;
* flapping;
* open alerts (incidents, not notices; only for users with `alerts.view`);
* interfaces that are down unexpectedly;
* template groups that need attention (one issue per row, or a count when
  more than three rows of a group do);
* maintenance (informational, not counted).

A FortiGate can therefore be **Up · 3 issues**: an IPsec tunnel down and four
FortiAPs offline are visible at a glance even though no alert rule covers them.

## Template readings

`app.services.device_insights.shape_template_groups` shapes
`device_template_values` for both the template-insights and summary APIs.

* **Not applicable.** A group can declare when it applies:

  ```json
  {"key": "ha_members", "applies_when": {"group": "ha", "metric": "fgt_ha_mode",
   "not_in": [1], "reason": "HA mode is Standalone"}}
  ```

  `in` and `not_in` take enum codes. An unknown controlling reading keeps the
  group applicable. Built-in templates get the same rule from
  `BUILTIN_APPLICABILITY` (HA members on a standalone FortiGate). A group that
  does not apply has status `na`, is shown as "Not applicable" and is left out
  of health and issues.
* **Missing is not zero.** In a table row whose own state is offline, down, not
  present or similar, gauge readings (CPU, memory, clients) are blanked and
  shown as "—" ("not reported while offline"). Rate counters (tunnel traffic)
  are kept. A zero "capacity" reading is treated as not reported.

## Identity

The summary API resolves model, OS version, firmware and serial once, with the
source: the normalised inventory identity, then the device record, then the
template reading, then the ENTITY-MIB chassis row. The header and the
Configuration tab show the same value.

## Interfaces

Only **unexpected** down ports are actionable: administratively up,
operationally down, monitored, and seen up at least once (`last_up_at`, the
same baseline as the Interface-down alert). Admin-down, never-used and
not-present ports are counted separately and are not failures.

## Availability

The Summary headline ("Checks answered") uses the shared uptime-stats
calculation narrowed with `device_id`, with two decimals. Failed checks remain
diagnostic evidence; confirmed outages come from the confirmed transitions.
Gaps are unknown, never healthy time.

## Display rules

One display timezone (the browser's, named once in the range bar); one
absolute and one relative time format; numbers to three significant figures
with units; percentages of CPU and memory as whole numbers; missing values as
"—". Percent sparklines use a fixed 0–100 scale. Trends are absolute deltas
against the range average and are coloured only past a significance band or a
threshold.

## Vendor tabs

A template groups its domain readings into tabs by declaring `tab` on a group:

```json
{"key": "ipsec", "name": "IPsec Tunnels", "kind": "table", "tab": "VPN & SD-WAN", "metrics": [...]}
```

`"tab": null` (or an empty string) keeps a group in the default "*Vendor* metrics"
tab. A group that declares nothing gets the built-in tab for its key from
`device_insights.BUILTIN_TABS`: IPsec, SD-WAN, SSL-VPN and GlobalProtect go to
*VPN & SD-WAN*; FortiAP and wireless controller groups to *Wireless*; F5 pools,
pool members, nodes and virtual servers to *Pools & virtual servers*; session
tables to *Sessions*; VDOMs and virtual systems to their own tab. Hardware
groups stay on Hardware unless the template declares otherwise.

The summary (`template.tabs`) and template-insights (`tabs`) APIs list the
declared tabs that have readings, in template order, with their groups; each
group digest carries its `tab`. The tab URL id is `tpl-<slug>`
(`?tab=tpl-vpn-sd-wan`). Issues, health groups and `?group=` links open the tab
that shows the group; an unknown tab id opens Summary. A declared tab keeps its
own widget layout (section = the tab id).

## Related

`GET /devices/{id}/related` (devices.view, device scope) answers the Related
panel on the Summary tab:

* **Depends on**: the managing controller (`managed_by_device_id`) and upstream
  topology dependencies (with type and whether they suppress alerts).
* **Depends on this device**: managed devices (counts by status; the ones not
  Up are listed) and downstream dependencies.
* **On network maps** (maps.view only): the maps that show the device, each
  linked as `/maps/manual?map=…&mode=live&focus=<device id>` (the map opens
  centred on the device with it selected), and the devices linked to it there.
  No LLDP/CDP neighbour table is collected, so map links are the neighbour source.
* **Changed at the same time**: devices whose confirmed status changed within
  ±2 minutes of one of this device's last five confirmed transitions (last 30
  days), from `device_status_log`, with the offset ("Down 40 s after this device
  went Down"). The log is only read.

Every listed device is filtered by the caller's tag scope. A section whose
source is unavailable is `null` and named in `unavailable`.

## Owner and notes

`devices.owner` and `devices.notes` (migrate-165) are free text set through
`PUT /devices/{id}` with devices.manage; blank clears. The owner shows in the
header identity strip; both show on the Configuration tab.

## Saved layouts

Widget layouts are saved on the server per user, per device or vendor/type
template, and per tab (`device_widget_layouts`, migrate-165):
`GET /devices/{id}/widget-layouts?vendor=&device_type=`, and
`PUT`/`DELETE /devices/{id}/widget-layouts/{section}` with `scope` `device` or
`template`. Saving one's own layout needs only devices.view. The browser keeps
a copy so the page renders at once: a copy marked `synced` mirrors the server;
an unmarked one (saved before this release or while the server was
unreachable) is uploaded the first time the server has nothing for that slot;
a synced copy the server no longer has was reset elsewhere and is dropped. The
rules are unchanged: a device layout wins over the vendor/type template, an
empty layout is the "standard layout" override, and entries for temporarily
absent widgets are kept.

## Alert names

Device status alerts are named after the condition that triggered them, not the
rule name and new status:

| Transition | Title |
|---|---|
| to Degraded by loss | `Packet loss 33% (limit 10%)` |
| to Degraded by latency | `Latency 412 ms (limit 100 ms)` |
| both | `Packet loss 33% (limit 10%) · latency 412 ms (limit 100 ms)` |
| window degradation, latest check within limits | `Latency or packet loss over limits on recent checks` |
| to Down | `No response` |
| back to Up | `Responding again` / `Latency and packet loss back within limits` |

The stored message is `<condition> — <host> (<ip>)` and the alert metadata
carries `condition`, `condition_metric`, `condition_value` and
`condition_limit` (`app.services.alert_condition`). The limits are the status
policy's Degraded limits and the over-limit test is the status machine's own
predicate; which rules match, thresholds and the device-status contract are
unchanged. On the device's own page the title drops the repeated host and IP;
alerts raised before this read "<rule> · Degraded". The rule stays visible
beside the title.

## Repeated alerts

On the device's own page, repeats of one condition from one rule are one row
instead of one row per alert (field review D-09): `Degraded (packet loss) ×5
in 3 days`, `No response ×2 in 5 hours`. This is presentation only. Alert rows,
rules, thresholds, notifications and the confirmed status are unchanged, and
every alert stays reachable at `/alerts/{id}`.

* **Key**: the rule (`rule_id`, else its name), `metadata.condition_metric`
  (`packet_loss`, `rtt`, `reachability`, `performance`), `metadata.new_status`
  and, for per-interface rules, `metadata.if_index`. Legacy rows without a
  condition metric group by rule and new status. A recovery (new status Up) is
  never grouped with its outage. Latency and packet loss are separate groups.
  An alert without a rule is never grouped.
* **Label**: one title shared by every alert in the group is the label (`No
  response`). Differing readings use the status and what was measured
  (`Degraded (packet loss)`). Otherwise the label is the rule name, or failing
  that the newest title. Then `×<count> in <span>`, where the span runs from
  the first alert to the newest, rounded up to minutes, hours (under 2 days) or
  days.
* The key and label rules are in `app.services.device_insights` (summary API)
  and `pages/device/eventsModel.ts` (Events tab). Both replay
  `scripts/dev/device-alert-groups/vectors.json`, so change both
  implementations and the vectors together.

Where it applies:

* **Events tab**: the type, severity and text filters apply to each alert first,
  so a group holds only the alerts the filters show (a lone match is a plain
  row). A group takes its newest alert's place in the timeline, the worst
  severity among its alerts and the most urgent workflow state (active, then
  acknowledged, then resolved), and shows the latest reading, the rule and how
  many alerts are still open. Expanding a group lists every alert with its
  link: the first 10 are shown, then *Show all*. Paging (*Show 50 more*) counts
  rows. The events chart and the chip counts still count every alert. *Group
  repeats* turns grouping off (`?egroup=off`).
* **Issues bar**: the summary API groups the open (active or pending) alerts,
  so the issue count counts conditions, not alerts. A repeated condition reads
  `Degraded (packet loss) ×3 in 2 days · latest: Packet loss 38% (limit 10%)`
  and opens the device's open alerts from that rule (`/alerts?device_id=…&rule_id=…&view=open`).
  A single alert still opens its own page. The active-alert count beside the
  bar still counts alerts.
* **Summary "Recent events"**: `alerts.groups` in the summary groups every open
  alert the API reads (at most 50). A repeated condition is one row that
  expands to its alerts with their links. A single alert links to its page.

## Performance comparison and export

"Compare with the previous period" (`?compare=previous`) overlays the window
just before the current one, shifted onto the same time axis and drawn dashed,
and states the change of the average in the metric's own unit ("+3 pt", "−22
ms"). Missing samples on either side make the change unknown, never zero. SNMP
readings use `GET /devices/{id}/snmp-metrics?hours=N&offset_hours=N`.
"Export PNG" draws every chart with its statistics line onto one image in the
current theme; nothing leaves the browser.

