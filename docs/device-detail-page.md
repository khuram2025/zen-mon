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
| Summary | KPIs (checks answered, latency, CPU, memory, a vendor highlight such as sessions or PoE, interfaces), latency and loss against the Degraded limits with the check strip, recent events, WAN and busiest interfaces, health groups with values, NetFlow. |
| Performance | Synced charts (CPU, memory, latency, loss, sessions, temperature) with thresholds and min/avg/p95/max; CSV export. A reading opened from another tab is pinned at the top. |
| Interfaces | The interface table inside the device shell (`/devices/:id/interfaces` redirects here). |
| *Vendor* metrics | The template's own groups (tunnels, SD-WAN, APs, VDOMs, pools…). |
| Hardware | Template hardware groups (sensors, PSU, fans, temperature, stack, PoE, memory pools, disks, optics) and the ENTITY-MIB component inventory. |
| Events | One range-bound timeline of alerts, status changes, flapping, traps, reboots and maintenance. |
| Configuration | Identity and monitoring, monitoring sites, the status policy, configuration backups and connected endpoints. |

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
