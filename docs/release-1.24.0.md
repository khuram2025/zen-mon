# ZenPlus 1.24.0 — Inventory: operating systems, software, hardware and change history

ZenPlus now brings what it already collects about your servers and network
devices into one inventory. Everything below ships in the appliance package;
separately installed Windows agents are untouched and need no update.

- **A new Inventory & Compliance group** (shown as *Inventory* in the collapsed
  sidebar) with **Overview**, **Assets**, **Operating Systems**, **Software**,
  **Hardware**, **Changes** and **Inventory Settings**. It answers *how many of X
  do we have*, *which machines run version Y* and *what changed*. Nearly every
  count is a link to the list of machines behind it, and the list has exactly that
  many rows. Nothing needs configuring: it is built from the Windows agent and the
  SNMP poller you already run.
- **Operating systems and firmware.** Server operating systems and network
  firmware as a family, release and version tree, with the share of machines on
  the newest version seen on your own estate ("build currency").
- **Software.** Every installed product as one row with host counts and the
  spread of its versions; a product page with the servers it is installed on, a
  timeline and the raw names grouped into it. Updates, drivers, operating-system
  components and runtimes are hidden by default.
- **Endpoint protection.** The Overview shows how many servers have an antivirus,
  endpoint-protection or EDR product installed, or a running Microsoft Defender
  service, and lists the servers without. A server whose software list is
  missing, cut short or not processed yet, or that has reported no service list
  at all (so a running built-in Defender cannot be seen), is shown as *unknown*,
  never as *without*.
- **Hardware.** Network vendors and models with serial-number coverage, a
  models-by-firmware matrix, chassis and module groups, and CPU, memory and disk
  distributions for servers.
- **Change history, from the first start.** Installs, removals, upgrades,
  operating-system changes, firmware changes and hardware swaps are recorded as
  they are reported, kept for 400 days by default, and shown on the Changes page
  and in each machine's flyout. History cannot be reconstructed, so it starts when
  this release is installed, and the Changes page prints that date.
- **Honest data.** Every machine shows when it last reported (Fresh, Aging, Stale,
  Never). A machine that cannot report an inventory by design (a ping-only
  device, a server monitored without the ZenPlus agent) is shown as *Not reported
  by design*, counted, and kept out of the "never reported" worklist. Machines
  whose operating system, vendor or model is unknown are listed, counted and
  clickable. An *Inventory health* panel lists eight gaps as worklists. While
  software lists stored before the upgrade are still being processed, every
  Inventory page says so.
- **CSV export** of every list, with the new sensitive permission
  `inventory.export`; every export is audit-logged, and an export is refused
  when the audit entry cannot be stored. The *first seen* columns of the software
  exports are stored values: for software that was already installed when
  Inventory started recording they are not the day Inventory saw it.
- **Documentation.** The (i) icon on each Inventory screen opens the matching
  article at zentryc.com/kb/zenplus/inventory.
- **Existing pages link into Inventory.** The Servers menu entry *Inventory* is now
  **All Servers** (same page), so "Inventory" means one thing. The Devices list can
  show **Vendor**, **Model** and **OS version** columns (off by default, from the
  *Columns* menu) and filters by Vendor and OS version; a device's operating
  system or firmware on its detail page opens the list of devices on the same
  release; a server's Software tab links each package to its Inventory product and
  warns when the agent's list was cut; and the Server Overview's operating-system
  panel links to the Inventory operating-system page.
- **For scripts and exports that read the Devices list.** The CSV export of the
  Devices page (built in the browser from the list on screen) keeps its 13 columns
  in place and gains three at the end, headed `vendor`, `model` and `os_version`
  whether or not their columns are shown; a script that reads the file by position
  is unaffected, one that reads it by header sees three new names. `GET /devices` gains the filters
  `vendor`, `model` and `os_version`, the option `facets=true` (value counts over
  every device the caller may see, not the page) and a field `os_version_display`
  on every item (the normalised Inventory version where there is one, else the
  device's own `os_version`, which is unchanged). The Vendor and OS version counts
  of the Devices page can differ from Inventory's, because the Devices page counts
  the device's own stored values.
- **Action needed for custom roles.** New permissions `inventory.view`,
  `inventory.export` and `inventory.manage`. Built-in roles are updated
  automatically; **custom roles are never changed and must be granted
  `inventory.view` by hand**.
- **Right after the upgrade, give it time.** Existing software lists are processed
  in the background, about two minutes per 100,000 software rows; until
  that has finished the figures are marked incomplete.

Schema: `migrate-157-inventory-foundation.sql` (new tables
`inventory_identity`, `inventory_change_events`, `inventory_daily_snapshots`;
evidence columns on `devices`, `servers` and `server_software_inventory`; capture
triggers), `migrate-158-inventory-phase1-followups.sql` (capture start, freshness
of servers that already reported, first-seen repair, three partial indexes) and
`migrate-159-audit-actor-role-width.sql` (`audit_logs.actor_role` widened to 50
characters so that long custom role names are audited). Appliance-only, minimum
prerequisite 1.23.9. Full notes, the first-hour guide and how to go back:
`Documentation/Inventory/06-RELEASE-NOTES-DRAFT.md`.
