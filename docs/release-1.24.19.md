# ZenPlus 1.24.19 — Device page redesign

The device page now answers three questions in order: is the device healthy, what is wrong right now, and where to go next. See `docs/device-detail-page.md`.

- **Header:** the confirmed status (unchanged rules) sits beside a separate health rollup, for example *Up · 3 issues*. Model, OS, serial, uptime, site and group are in one identity strip. One action bar: Check now, Diagnostics, Maintenance, Acknowledge (only when alerts are open), and backup, configuration history, web UI, copy link, edit and delete.
- **Issues bar:** open problems worst first. A FortiGate with an IPsec tunnel down and four FortiAPs offline now shows both at the top, even when no alert rule covers them.
- **Tabs by task:** Summary, Performance, Interfaces, the vendor's metrics, Hardware, Events and Configuration. Each tab loads its own data only while open. Old links still work.
- **Interfaces** open inside the device page, with the alias, an operator status (Down, Admin down, Unused, Not present), type groups, saved filters, and errors and discards for the selected period.
- **Events** is one timeline of alerts, status changes, flapping, traps, reboots and maintenance, filtered by type and severity and bound to the selected period.
- **Performance** shows synced charts with configured thresholds, min/avg/p95/max and a CSV export. A reading opened anywhere else is pinned here.

Readings you can trust:
- HA members on a standalone unit are *Not applicable* and are left out of health. Templates can declare `applies_when`.
- Offline APs and other absent rows show "—", not 0% CPU or 0 clients. A zero capacity is not reported.
- Every tab shows the same OS version, serial and model, with its source.
- Percent sparklines use a fixed 0–100 scale, so a flat 45% CPU is flat. Trends are absolute changes against the range average and are coloured only when significant.
- Interface charts no longer clip their axis labels (160 Mbps no longer reads as 60.00).
- "Interfaces up" separates unexpected down ports from unused and admin-down ones.
- One display time zone and one time format. Tiles keep their values while refreshing.
- A NetFlow exporter whose every flow is classified as one application is flagged instead of reported as a finding.

API: `GET /devices/{id}/summary` (new), `GET /devices/{id}/interfaces/error-totals` (new), `device_id` on `/devices/dashboard/uptime-stats`, and `ever_up`/`last_up_at` on `/devices/{id}/interfaces` (additive). No migrations. No changes to status confirmation, alert rules or customer thresholds.
