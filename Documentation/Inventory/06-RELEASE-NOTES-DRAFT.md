# 06 — Release notes draft: Inventory (Phase 0 and Phase 1)

*Status: draft · 2026-10-09 · rewritten after the Phase 1 review fix round and updated after its re-check (round 2), from the final code. Written for the release that ships Phase 0 (foundations and change capture, merged 2026-10-08) and Phase 1 (the Inventory dashboard) together. Nothing in it has been merged, tagged or published yet: every statement about the release itself is planned. Section 1 is the text for `docs/release-<version>.md`, in the style of `docs/release-1.23.35.md` (its heading becomes the H1 of that file). Sections 2 to 9 are the permissions, history, schema, first-hour, upgrade and rollback, operator-checklist, known-limitations and support notes that go with it. Section 10 is for the maintainers and is deleted when the notes are published. The version number below is the one proposed in `04-ROADMAP-PHASES-AND-TASKS.md` and is set when the release is tagged. Nothing is published by this draft.*

---

## 1. Release notes text

### ZenPlus 1.24.0 — Inventory: operating systems, software, hardware and change history

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
  Inventory started recording they are not the day Inventory saw it (section 3).
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
  the device's own stored values (section 8).
- **Action needed for custom roles.** New permissions `inventory.view`,
  `inventory.export` and `inventory.manage`. Built-in roles are updated
  automatically; **custom roles are never changed and must be granted
  `inventory.view` by hand** (see section 2).
- **Right after the upgrade, give it time.** Existing software lists are processed
  in the background, about two minutes per 100,000 software rows (section 5); until
  that has finished the figures are marked incomplete.

Schema: `migrate-157-inventory-foundation.sql` (new tables
`inventory_identity`, `inventory_change_events`, `inventory_daily_snapshots`;
evidence columns on `devices`, `servers` and `server_software_inventory`; capture
triggers), `migrate-158-inventory-phase1-followups.sql` (capture start, freshness
of servers that already reported, first-seen repair, three partial indexes) and
`migrate-159-audit-actor-role-width.sql` (`audit_logs.actor_role` widened to 50
characters so that long custom role names are audited). Appliance-only, minimum
prerequisite as in the signed manifest.

*[To confirm before publishing: the version number and date, and the minimum
prerequisite version.]*

---

## 2. New permissions, built-in roles and custom roles

| Permission | Grants | Risk |
|---|---|---|
| `inventory.view` | Every Inventory page, within the user's tag scope; Inventory Settings read-only | Standard |
| `inventory.export` | CSV export of inventory datasets (serial numbers and software lists are reconnaissance data) | **Sensitive** |
| `inventory.manage` | Changing Inventory Settings: freshness targets, history retention, snapshot time, trends switch, hidden categories | Standard |

In the role editor, ticking export or manage also adds view. The server checks each
permission on its own when a request arrives.

**Built-in role definition version 6.** At the first start of the new release the
built-in roles are reconciled once, under an advisory lock. For every built-in role
(the rows the application itself maintains, which cannot be edited or deleted in
the UI) whose stored definition version is below 6, the role's permissions are set
to the current definition, and one *builtin_reconcile* entry in the role history
lists exactly which permissions were added and removed and how many users hold the
role. An appliance still on 1.23.35 is at definition version 4, so it moves to 6 in
one step and receives both the version 5 grants (Operator, Viewer, Read Only) and the
version 6 grants (the three team roles) in that one reconcile:

| Role | View | Export | Manage |
|---|---|---|---|
| Admin, Operator | yes | yes | yes |
| Viewer, Read Only | yes | no | no |
| Network Team, Systems Team, Security Team | yes | no | no |
| Application Team | no | no | no |

Reasoning (owner recommendation D9): network, systems and security teams work from
the estate inventory; the Application Team works from applications. Nobody but
Operator and Admin exports or changes settings.

**Custom roles are never changed by an upgrade.** A role an administrator created,
including a copy of a built-in role, keeps exactly the permissions it has. After
upgrading:

1. List the custom roles whose members should see Inventory (roles copied from
   Operator, Viewer or the team roles, and auditor and security-staff roles).
2. In *Settings > Users & Access > Roles & Permissions*, edit the role and, in the
   *Access* step, set the *Inventory* module to *View*; tick `inventory.export` only
   for those who must download CSV files; choose *Full* only for administrators of
   the module. (The *Review* step previews the menu the role will see.)
3. A role limited to tags (its visibility scope) sees only assets carrying those
   tags, and **untagged assets are invisible to it**. Check the tags.

**Role descriptions.** The reconcile updates permissions, not text. The one-line
descriptions of Network Team, Systems Team and Security Team in the role list were
written before Inventory existed; they end with *Includes read access to Inventory
(inventory.view)* only on an installation where the role row is created by this
release, and an existing appliance keeps its old description text. The permission
matrix of the role is the source of truth.

`inventory.view` shows what is installed on servers (the software list of a server
is otherwise behind `servers.view`). This is intended, within the tag scope, and is
the reason export is separate and sensitive.

---

## 3. What is recorded from the first start

History cannot be backfilled and ZenPlus never invents it. From the moment the
release is installed:

- **Software** installs, removals and version changes on servers that report through
  the Windows agent; **operating-system** version changes on servers; **firmware**
  version changes on network devices; **hardware** model or serial-number changes
  (a chassis swap).
- **Not recorded as changes:** the first report of a server (its baseline; servers
  that already held a software list at upgrade were baselined by the migration), a
  value appearing for the first time, a value going missing, deleting a server, and
  any re-reading of the same data caused by an update to the normalisation rules.
- **History starts at the capture start.** Migration 158 stores the time at which
  migration 157 was applied as the capture start. The Changes page footnote prints
  that date (*History starts … when change capture began. Changes older than 400 days
  are removed.*), not the date of the first recorded change; on a quiet estate the
  first change can come days later and the days between are covered, not gaps. After
  the retention period passes the capture start, the footnote says *Change capture
  began … The history you can see therefore starts …*.
- **Bounds.** The agent sends at most 1,000 installed titles per server (sorted by
  name); changes recorded while a list is capped carry a note, and titles that fall
  off the end of a capped list are not treated as removed. A server is budgeted at
  about 2,000 software changes per rolling 24 hours: the budget is checked **once per
  report, when the report arrives**, so a report that starts below it is recorded
  whole and one large report can take a server past 2,000; once a server is at or
  over it, the software changes of further reports are not written to history and one
  *Change budget exceeded* row marks the day (the inventory itself stays correct).
  Operating-system, firmware and hardware changes are not budgeted.
- **Retention** is 400 days by default (30 to 3,650 in Inventory Settings), purged
  daily. Daily count snapshots, which feed the trend lines, are kept three years.
- **Freshness at the upgrade.** A server that already held a software list gets the
  time of that list (migration 158: the newest `updated_at` of its software rows,
  which every agent upload rewrites) as its last inventory, so it shows its true age,
  *Stale* if it stopped reporting long ago. A network device reads *Never* until its
  next successful SNMP poll. An agent server that never delivered a list reads
  *Never* until its next inventory report (every 6 hours by default). A server
  monitored without the ZenPlus agent, and a ping-only or controller-managed device,
  reads *Not reported by design*: it cannot send an inventory, so it is listed and
  counted but is not *Never* and is left out of the fresh percentage.
- **First seen.** Migration 157 stamped every existing software row with the time of
  the migration. Migration 158 repairs those rows from the install date the machine
  itself reported when that date is plausible (after 1995 and earlier than the
  stamp). The pages show a first-seen at or before the capture start as *before
  <capture date>*, with the earliest known date in the tooltip, never as an install
  day. Software that appears after the capture start shows the day it was first seen.
  **The CSV exports carry the stored value with no marker** (`first_seen` of the
  software dataset, `first_seen_at` of the installations dataset): for a row at or
  before the capture start it is the install date the machine reported (for example
  `2024-08-04T17:15:03Z`) or the time of the upgrade, so it is not the date Inventory
  first saw the product. Compare it with the capture start (the footnote of the Changes
  page, `history_start` in `GET /inventory/changes`) before using it as an observation
  date; only a value after the capture start is the day the product first appeared. The
  export article says so.
- **Trend lines** appear on the Overview after three daily snapshots for *Assets*,
  *Software titles* and *Fresh inventory*, and one day later for *Changes per day*
  (today's count is still being written, so that line stops at yesterday and needs
  three complete days). They are not shown to users limited to tags. The first
  snapshot is held until the services have been up for 30 minutes and the normalising
  backlog is empty (at most 12 hours), and the current day's row is refreshed hourly
  during the first 24 hours after the capture start, so the series does not begin with
  the half-finished state of the upgrade.
- **A few version changes may appear on the first software report after the upgrade.**
  A server whose list names the same program twice with different versions (for example a
  32-bit and a 64-bit entry) used to keep the last one listed; it now keeps the highest, so
  each such program records one change.

---

## 4. Schema, services and background tasks

- **Migration order.** 157, then 158, then 159, the order of `scripts/migrations.lock`
  (which, not the file name, fixes the order). All three are idempotent. The signed
  update applies them with the services stopped and the schema gate
  (`scripts/sync-schema.py`) verifies the result before the new version is recorded.
- **Migration 157** (`migrate-157-inventory-foundation.sql`) is idempotent and safe to
  re-run. It adds `inventory_identity` (one normalised identity per asset, kept current
  by a background task), `inventory_change_events` (append-only, no foreign keys, so
  history outlives a deleted asset) and `inventory_daily_snapshots`; evidence columns
  (`devices.sys_descr`, `devices.identity_updated_at`, `servers.inventory_*`,
  `servers.software_truncated`, hardware evidence columns, `server_software_inventory`
  architecture, source, install location, first-seen, normalised keys and category); and
  the capture triggers. It seeds an identity row for every existing server and device and
  baselines servers that already hold software. Every column it adds to an existing table
  is nullable or has a default.
- **Migration 158** (`migrate-158-inventory-phase1-followups.sql`) changes no table
  shape. It (1) stores the capture start as `system_settings['inventory.capture_started_at']`,
  taken from the ledger time of migration 157; (2) sets `servers.inventory_updated_at` from
  the newest software row for every baselined server that has none; (3) repairs
  `first_seen_at` of rows stamped by 157 from their install date; and (4) creates three
  partial indexes (`idx_ssi_endpoint_protection`, `idx_ssvc_endpoint_protection`,
  `idx_servers_device_id`) for the endpoint-protection and presence lookups. Step 3 is one
  `UPDATE` over the rows that carry an install date, inside the migration transaction, with
  the capture triggers suppressed: about 17 to 20 seconds at 2 million software rows on the
  scratch cluster; plan for longer on a slower disk, and the rewritten rows leave dead
  tuples that autovacuum reclaims afterwards.
- **Migration 159** (`migrate-159-audit-actor-role-width.sql`) widens
  `audit_logs.actor_role` from `VARCHAR(20)` to `VARCHAR(50)`, a catalog-only change
  (no table rewrite). Before it, the audit entry of any action by a user whose role name
  is longer than 20 characters failed to insert and the action went unaudited; Inventory
  export and settings changes refuse to proceed without their audit entry, so the column
  has to hold any role name.
- **Brief exclusive locks.** Migration 157 takes exclusive locks on `devices`, `servers`
  and `server_software_inventory` for about **0.5 to 1.5 seconds** at 5,000 servers by
  400 packages (measured on a scratch database; to be confirmed on the canary). The signed
  update runs the migrations with the services stopped, which is why nothing queues behind
  the locks. **Do not apply migrations 157 or 158 by hand to a running appliance**: the
  locks and the repair would stall the poller and the agent upload path.
- **Poller write-order change.** The Go poller now writes a device's chassis entities before
  its system information, stores `sysDescr`, and stamps `identity_updated_at` on every
  successful poll. This stops a device's first poll from recording a model change that never
  happened. A poller that starts before the migration has run falls back to the old
  statement and logs a warning (section 7).
- **Ingest hardening** in the agent upload path: same-name entries are reduced to one
  (highest version), architecture, source and install location are kept, the 1,000-title cap
  and a 5,000-entry server-side bound are enforced, an out-of-order snapshot cannot rewind
  state, and the per-server change budget applies. Every inventory step is isolated so it can
  never fail an upload.
- **Background tasks** start with the API in every worker: the normalise task (45 s after
  start, then every two minutes after each run; batches of 500 identity rows and 2,000
  software rows; at most 40 identity and 50 software batches per run; advisory lock
  1515074397), the daily snapshot and retention task (wakes every five minutes; writes at 15
  minutes past the configured hour, appliance time zone, default 00:15; advisory lock
  1515074398) and a catalog warmer that keeps the whole-estate software table fresh in the
  background (switch it off with `ZENPLUS_INVENTORY_CATALOG_WARMER=0`).
- **Exports** hold a pooled connection and a database snapshot while they stream, so they are
  admission-controlled: at most **one export per user and two in all, counted per API worker
  process** (`429` with `Retry-After: 10` beyond that). The counters are in memory and the
  appliance starts two API workers (`uvicorn --workers 2`), so a request lands on either of them:
  a person who starts a second export while the first is still running is refused only when it
  reaches the same worker, so two at once are possible, and the appliance as a whole can run up to
  four. The limit protects the connection pool of each worker, which is what it is for. A
  download that is not read for 120 seconds is abandoned; a client that disconnects in the middle
  of a slow download can keep its slot until that timer ends (see `07-PHASE1-FOLLOWUPS.md`).
  A file is read in one `REPEATABLE READ` read-only transaction, so the assets, installations,
  operating-system, hardware and change files are one instant. The **software** file is built from
  the catalog's in-process read model: its rows and their order are consistent, but each figure is a
  value the catalog showed within the minutes around the file, not one instant.
- **No new service, port, credential or setting file.** `.env.example` is unchanged. The one
  environment switch above is optional.

---

## 5. The first hour after the upgrade

Raw names and versions stored before Inventory existed are normalised once, in the
background. The pages are usable at once, say so while it runs, and fill in as it goes.

**Duration.** One normalise run keys at most 100,000 software rows (50 batches of 2,000)
and up to 20,000 identity rows (40 batches of 500). The first run starts 45 seconds after the
API starts; later runs start two minutes after the previous one ended, and a run of 100,000
rows takes about 6 seconds on the scratch cluster (longer on slow disks). With `rows` = servers
× software titles per server:

> time until the software figures are complete ≈ 45 s + (⌈rows / 100,000⌉ − 1) × (120 s + about 6 s)

The API runs two workers, each with its own loop, and they share the work, so this is an
upper bound.

| Estate | Software rows | Runs | Complete after (about) |
|---|---|---|---|
| 100 servers × 250 titles | 25,000 | 1 | under 1 minute |
| 500 servers × 400 titles | 200,000 | 2 | 3 minutes |
| 1,000 servers × 400 titles | 400,000 | 4 | 7 minutes |
| 2,500 servers × 400 titles | 1,000,000 | 10 | 20 minutes |
| 5,000 servers × 400 titles | 2,000,000 | 20 | 41 minutes |

Network devices, servers without a software list and the identity rows (up to 20,000 assets)
are done in the first run. The backlog count that the message shows counts exactly the rows
this loop will pick up: a row it never selects (for example a row without a source on a server
that is not a Windows machine) is not backlog, so it never keeps the message on screen.

**What the operator sees, in order**

| When | What is on screen |
|---|---|
| Services up, before the first run (about 45 s) | Every Inventory page shows *Normalising existing software lists; figures are incomplete*, with the number of software rows and assets still to process at the last check; the page checks again every 15 seconds and refreshes its figures once when the backlog has cleared. The software catalog is empty and says the lists are not processed yet. Operating systems of servers and devices read *Unknown* until their identity rows are normalised. Endpoint protection reads *unknown* for every server with a list, never *without*, so there is no false worklist of "servers without protection". The Overview shows no all-clear sentence |
| Each run | Identity rows first, then software rows. Products, installs, versions, publishers and endpoint-protection coverage grow run by run; a smaller number is not a finding while the message is up |
| Backlog empty | The message disappears. The catalog, the sprawl panel and the coverage line are complete. Servers that never sent a list, servers whose list was cut at the cap, and servers that have reported no service list at all stay *unknown* for endpoint protection and are not counted as *without*; each names its reason in the flyout |
| 30 minutes after the start, backlog empty | The first daily snapshot may be written (at most 12 hours of holding), and the current day's row is refreshed hourly for the first 24 hours |
| Hours | Devices move from *Never* to *Fresh* at their next SNMP poll; agent servers at their next inventory report (default every 6 hours) |
| Day 3 or 4 | Trend lines (section 3) |

The same applies after any later release that changes the rule packs: the revision is
recorded on every row, rows of an older revision are re-keyed in the background without
writing history, and the message appears again for the minutes it takes.

---

## 6. Upgrade notes and how to go back

### Upgrade

1. **Order: migration, then services.** The signed update stops the services (API, poller,
   NetFlow collector and the rest), takes a backup of the code and the database, replaces the
   code, applies the migrations (157, 158, 159), runs the schema gate, and only then starts the
   services again; a health check follows. Do not start the new application against the old
   schema, and do not apply the migrations separately to a running appliance.
2. **Canary first.** As for every release, publish to the canary group, check the list in
   section 7 on it, then promote. A database dump of your own (`sudo zenplus backup`, written
   to `/var/lib/zenplus-appliance-backups`) is worth taking before the update; restoring it
   is a support-guided operation too.
3. **Windows agents** are untouched. The agent in this release does not report a server's
   manufacturer, model, serial number or BIOS, so those fields are blank for servers.

### How to go back

There are three different situations. Read them in this order.

1. **The update fails its own health or schema check.** Nothing to do. The updater restores
   the code and the database from the backup it took for that attempt and starts the old
   version again (the manifest's `rollback_steps`: restore the backup, then start the
   services). This is application recovery, not an operating-system rollback.
2. **The update succeeded and you want the previous version back.** There is **no
   operator command and no self-service downgrade**. `zenplus backup` writes a
   database-only dump (`pg_dump`, gzip, root-only) to `/var/lib/zenplus-appliance-backups`;
   it is not the updater's backup and it is not a restore command either. The updater's
   restore (`scripts/ota-restore-backup.py`) runs as root from the signed payload of that
   update.
   Contact support, who restore the pre-update backup the updater took, found under
   `/var/lib/zenplus-updater/backups`. Know what that costs before asking:
   - the restore **drops every user schema in PostgreSQL and replays the dump**, so everything
     written to PostgreSQL since the update is lost: agent reports, discovered devices,
     alert, configuration and user changes, audit entries and Inventory history;
   - **ClickHouse is not restored** (ClickHouse migrations are not reversed; this release adds
     none);
   - only the **three newest** backups are kept, so the backup of this update is deleted after
     three further updates; and
   - the restore brings back the previous application together with a database that has none
     of the Inventory objects, which is why it is the only supported way back.
3. **Do not install the previous package over the upgraded database.** The new tables,
   columns, triggers and indexes are additive (every column added to an existing table is
   nullable or has a default, migration 158 changes no shape and migration 159 only widens a
   column), so the previous code would run and ignore the new objects. But the capture triggers
   fire for whatever writes the rows, and the previous agent upload path lacks the ingest
   hardening: it overwrites a version without keeping the known one, so two entries with the
   same name flap and write false *version changed* events, and it deletes every row an
   upload does not list, so a capped list writes false *removed* events. History written that
   way cannot be corrected. The previous version is tested against the previous schema only.

Fixing forward with a corrected release is the safe path: the schema is additive and the
history is kept.

---

## 7. Operator checklist after the upgrade

| # | Check | Expect |
|---|---|---|
| 1 | `sudo /opt/zenplus/scripts/sync-schema.py --check` | Exit 0: the schema matches the code (exit 2 means drift; details in `.schema-status.json`) |
| 2 | `sudo journalctl -u zenplus-poller --since "-30 min" \| grep "sys_descr"` | No `WARN: devices.sys_descr / identity_updated_at are missing` line |
| 3 | Sign in as an administrator: *Inventory & Compliance → Overview* | The group is in the sidebar; assets are counted; the message *Normalising existing software lists; figures are incomplete* shows for the time in section 5 and then disappears by itself |
| 4 | After the message has gone: *Software* and the Overview's endpoint-protection line | The catalog lists products; the line shows *covered*, *without* and *unknown* servers that add up to every server; servers without a software list, with a cut list or with no service list stay *unknown* (open one in the flyout for its reason) |
| 5 | The line under the tabs, a few hours later | *Fresh inventory* climbs as devices are polled and agent servers report; assets that cannot report are counted as *not reported by design*, not as *never reported* |
| 6 | *Settings → Users & Access → Roles & Permissions* | Operator, Viewer, Read Only and the Network, Systems and Security teams show Inventory access; **custom roles you want to see Inventory have `inventory.view`** |
| 7 | Sign in as a user whose role is limited to tags | The badge *Showing assets tagged …*; only those assets; no trend lines |
| 8 | *Inventory → Changes* after a day | The footnote names the capture start (the upgrade day) as the start of history; a few *version changed* rows on first reports are expected (section 3) |
| 9 | *Inventory → Overview → Inventory health* | Open each non-zero item once; devices missing a model or serial usually mean ENTITY-MIB is not answered |
| 10 | On day 4 (the *Changes per day* line one day later) | Small trend lines on the Assets, Software titles and Fresh inventory figures. If none appears for an unrestricted user with trends switched on, the snapshot task is not recording: see section 9 |
| 11 | Open the (i) icon on one screen | The knowledge-base article opens (it must be published on release day) |
| 12 | A first CSV export as a user with `inventory.export` | The file opens in a spreadsheet; the audit log has an `inventory.export` entry |
| 13 | *Devices* with the *Columns* menu | Vendor, Model and OS version can be switched on; the Vendor and OS version filters work |

If every asset is still *Unknown* for more than ten minutes after the upgrade, the normalise
task is not running: look for `inventory rule packs are not healthy` in
`journalctl -u zenplus-api`. That warning and the other warnings of the task (for example
*inventory normalise: waited too long for a lock*) are the only lines of the Inventory tasks
that reach the journal: the per-run summaries (`inventory normalise: … keyed`, `inventory
snapshot for …`) are written at information level, which the API's default logging does not
print, so their absence proves nothing. Use the on-screen message of check 3 instead.

---

## 8. Known limitations

- **Server hardware identity** (manufacturer, model, serial number, BIOS, physical or virtual)
  is not reported by the Windows agent in this release. The model, serial and vendor health
  checks therefore count network devices only, and the Hardware page says so.
- **Software inventory is Windows-agent only.** Linux servers and servers monitored without the
  agent have no software list.
- **Only the ZenPlus agent can send a server's inventory.** A server monitored over SSH, WMI,
  WinRM or SNMP only reads *Not reported by design* and never becomes *Fresh*; install the agent
  to include it. A server linked to an SNMP device takes its vendor, model and serial from the
  device but not its freshness; the age printed beside those three values in the flyout is the
  agent's last inventory, not the device's last poll.
- **Servers that stopped reporting still count in software figures.** The hosts, installs,
  versions, sprawl and endpoint-protection coverage include the last software list of every
  visible server, however long ago it was sent, until the server is deleted or reports again.
  The line under the tabs counts the stale and never-reported assets next to them, and the
  *Hosts* number of a product opens the list with each server's badge, but there is no setting in
  this release that excludes servers by age. Deleting a decommissioned server removes its software
  list from every figure at once; a server that is merely switched off keeps counting.
- **A server and its SNMP device are two assets unless they are linked.** Linking is done by a
  discovery import that holds both, or by setting `device_id` on the server through the API.
  Enrolling an agent does not link, nothing guesses the link from an IP address, and the
  Servers page has no control for it. A machine added both ways without a link is counted
  twice, once as a server and once as a device (a ping-only twin reads *Not reported by
  design*).
- **No lifecycle or compliance.** There are no end-of-life dates or policy results. *Build
  currency* compares versions with the newest seen on your own estate, not with the vendor's
  latest release; *Older* means two or more versions behind. The software catalog rules are
  read-only; unmatched titles are counted.
- **Endpoint protection** is defined on the catalog category *Endpoint protection* (antivirus,
  endpoint-protection-platform and EDR engines and sensors) plus a running `WinDefend` or `Sense`
  service. It shows that a product is installed, not that it is current or working, and a product
  the catalog does not know is not counted. *Without* needs a complete, processed software list
  **and** a service list: a server that has reported no service rows at all (for example one on an
  APM-only agent profile) is *unknown*, because built-in Defender shows only as a running service.
  Other security tooling never counts: zero-trust and SSE clients (Zscaler, Netskope), scanner agents,
  password managers, Sysmon, Wazuh and management agents are listed under *Security*, VPN clients
  (FortiClient, Cisco Secure Client) under *Remote access*, and the ZenPlus Agent under *Monitoring*.
  A server with a product from the category counts as covered even when its list was cut at the
  cap; the cap only prevents the verdict *without*.
- **Change history** begins at the capture start, software lists are capped at 1,000 titles per
  server, and a server's software history is budgeted at about 2,000 changes per 24 hours,
  checked once per report.
- **Version sprawl** is exact up to 400,000 visible software rows; above that the panel ranks
  the most installed products and says *N most installed of M products*. Windows redistributables
  that are installed side by side by design (the Visual C++ years) are separate products.
- **Speed at very large sizes.** At 5,000 servers by 400 packages (1.8 to 2 million software
  rows) every list, the facets, the operating-system and hardware pages and the summary stay
  within their budgets. The software catalog is the exception. The whole-estate catalog and
  publisher tables are kept warm by a background task, so a warm request takes 15 to 20 ms
  (publishers 3 to 5 ms). A process-cold request, which is the first one after an API worker
  starts or, for a user limited to tags, the first after that scope's table expired, takes 458
  ms (p50, 530 ms p95) for a scope of 63 percent of the estate against the 400 ms budget; large
  pages (`page_size=500`: 802 ms), deep pages by hosts (page 5: 558 ms) and the sorts that must
  rank every product (up to about 1.1 s, and about 3.3 s for `latest_version` on a catalog of
  24,000 products) also exceed it. An estate a tenth of that size answers every request in
  under 150 ms. The tables are in `05-P1-API-CONTRACT.md` section 6.5.
- **Time zones.** The server has no time zone database in its requirements: the zone of your
  browser is used for the day grouping when it is a zone the server knows (legacy browser names
  are mapped), and otherwise the Changes page groups days in UTC and says so.
- **Services pages in some time zones (pre-existing, not changed by this release).** The
  *Services* list asks for its daily bars, and the Services CSV/JSON reports ask for their days,
  in the browser's zone, and the server answers `422 Invalid reporting timezone` when the host
  cannot open that name. Chrome reports `Asia/Calcutta`, `Europe/Kiev`, `Asia/Saigon`,
  `America/Buenos_Aires`, `Asia/Katmandu` and `Asia/Rangoon` for browsers in India, Ukraine,
  Vietnam, Argentina, Nepal and Myanmar, and a host without the legacy zone names (the package
  `tzdata-legacy`) does not know them, so the bars read *History unavailable* and the reports
  fail for those users; the 24-hour availability headline still loads. Inventory maps these names
  and is not affected. Workaround: use a browser or operating-system zone with a current name
  (`Asia/Kolkata`, `Europe/Kyiv`); the fix is filed as a separate task.
- **Network devices.** MikroTik devices show no RouterOS version (it needs the `mtxrLicVersion`
  OID, which the poller does not read in this release) and their model is read from the
  description text, which is cut at the first space, so boards whose names differ after a space
  can share a model. The profile of earlier builds stored the board name (for example `RB4011iGS+`)
  as the version; the rule pack rejects it now, and the first normalise run after the upgrade
  clears a board name that an earlier build stored, without writing a change event (an
  appliance that never ran the unreleased Phase 0 build has none to clear). Cisco FTD and Aruba
  AOS-CX versions appear only when the device's description text or profile carries them. A
  child device managed through a controller (FortiAP, FortiSwitch) still lists *icmp* among its
  sources, although nothing pings it.
- **Devices page.** The key figures, charts and the *Filtered* line of the Devices page still
  describe the loaded devices (at most 200); a notice says *first N of M* when the list is cut
  short. The Vendor, Model and OS version columns are off by default, also for a user who had
  saved a column choice before they existed. The Vendor and Model menus count the device's own
  stored columns, and the OS version menu its `os_version_display`; Inventory's vendor facet counts
  the normalised vendor (aliases and spellings merged, so *Cisco* and *Cisco Systems* are one). The
  same estate can therefore show different counts for a vendor on the two pages; neither is wrong,
  and the list behind each number has that many rows.
- **Tag scope on server pages outside the module (pre-existing; the reads are now scoped).** The
  `GET /servers/{id}/…` read routes (metrics, processes, services, filesystems, network, events,
  software, compliance, commands, agent, network captures), `GET /agent-fleet` (list, counts,
  search and `/{id}`) and `GET /server-baselines/{id}/results` apply the user's tag scope: a user
  limited to tags who holds `servers.view` sees only the servers, agents and baseline results
  within the scope, and an id outside it answers `404` exactly as an unknown id does. An agent
  that is not linked to a server has no tags, so a user limited to tags does not see it in the
  fleet. Not covered: the baseline list and detail still show fleet-wide counts (servers
  evaluated, compliant, violations; no names), the capture detail routes
  (`/servers/network-captures/{id}/flows`, `/traffic`, `/summary`) answer for a known capture id,
  and the actions that need `servers.manage` do not check the scope. Until those are scoped,
  treat `servers.manage` as acting on every server, also for a role limited to tags.
- **Accessibility.** Support for forced-colors (Windows High Contrast) mode is incomplete: the
  data bars and the selected tab can lose their contrast there.
- **Exports.** One per user and two in all **per API process**; the appliance runs two processes,
  so up to two per person and four in all can run (`429` beyond the limit of the process that
  receives the request); an export is refused with `503` when the audit entry cannot be stored; a
  download not read for 120 seconds is abandoned, and there is no overall time limit on a download
  that is read slowly but steadily. The software file is not one instant (section 4).
- **Settings** is a single tab (*Data & retention*); this release has no catalog rules, saved
  views, asset attributes (owner, criticality) or lifecycle data.

---

## 9. Support

**What support asks for, and where to find it.** These five facts identify nearly every
Inventory question without a further round trip:

| Fact | Where |
|---|---|
| The asset address | In the Assets flyout the address bar holds `?asset=server:<id>` or `?asset=device:<id>` |
| What the flyout shows | The identity values with their source badges, the *Raw evidence* list and the freshness badge tooltip (last inventory and last seen) |
| The line under the tabs, and the message *Normalising existing software lists* with its numbers | Any Inventory page |
| The capture start and the retention | The footnote of the Changes page |
| The version, the schema state and the update log | `sudo /opt/zenplus/scripts/sync-schema.py --check` and `sudo journalctl -u zenplus-updater` |

**Logs.** `sudo journalctl -u zenplus-api --since "-1 day" | grep -i inventory` shows the
warnings of the Inventory tasks (rule packs not healthy, a batch skipped on a lock wait, a
snapshot written after being held for hours). The information-level summaries are not in the
journal (section 7). Agent upload problems are logged by the upload path as before. Every
export and every settings change is in the audit log (actions `inventory.export` and
`inventory.settings.update`).

**Common questions and the answer.**

- *Everything reads Never or Unknown.* Within the minutes in section 5 this is the upgrade; a
  device reads Never until its next poll. Beyond that, see the troubleshooting article.
- *A server appears twice.* It is a server and a device that are not linked (section 8).
- *A server never becomes Fresh.* It is not monitored through the ZenPlus agent (*Not reported by
  design*), or the agent's inventory collector is off or its interval is longer than the target;
  the interval is `collectors.inventory.interval_seconds` in the agent's own `agent.yaml` and is
  not a field of Agent Policies.
- *The endpoint-protection line looks wrong.* Open the server in the flyout: the pill lists the
  products and services that made it count, or the reason it is unknown: no software list (not
  reported yet, or not readable by the normaliser), a list cut at the agent's cap, a list still being
  processed, or no service list (built-in Defender cannot be judged). *Without* is shown only when
  a complete, processed list and a service list show neither a product nor a running Defender
  service.
- *No trend line after four days.* Check that the user is not limited to tags and that *Show
  trends on the Overview* is on; snapshots are recorded either way. If the sparkline is still
  absent for an unrestricted user, support checks the daily snapshot task.
- *Going back to the previous version.* Section 6: support-guided restore only, with data loss.

---

## 10. For the maintainers (delete before publishing)

1. Set the version, the date and the minimum prerequisite; run the release procedure in
   `/home/zen/releases/README.md` (clean tagged checkout, offline verification, canary, then full
   rollout only after the reference appliance accepts the exact signed package).
2. Publish the knowledge-base articles from `inventory-kb/` on release day, following
   `Documentation/26-GITHUB-MULTI-MACHINE-AND-PUBLISHING-RUNBOOK.md` section 8; read the
   *Before publishing* list in `inventory-kb/README.md` first. **All ten screenshots must be
   retaken** (they were taken before the review fix rounds; the README lists, per file, what is now
   different). The (i) icon in the product links to them.
3. Squash the local checkpoint commits for the pull request; all five required CI checks and
   conversation resolution apply. The dashboard job runs `test-inventory-ui.mjs`,
   `test-inventory-os-hardware-changes.mjs`, `test-inventory-software-settings.mjs` and
   `test-inventory-touchpoints.mjs`; the server job runs the contract generator's `--check` and
   `check_contract.py` (`scripts/dev/inventory-contract/README.md`).
4. Confirm the migration numbers against `origin/main` in `scripts/migrations.lock` before the
   pull request is opened (the lock ended at 157 when the fix round started; 158 and 159 are
   appended on this branch), and record the measured migration lock time of 157 and the runtime
   of the 158 repair from the canary in `04-ROADMAP-PHASES-AND-TASKS.md` (Phase 0 exit criteria).
5. Reference-appliance acceptance is still open and is required: identity coverage (vendor 95
   percent, OS version 90 percent, model 90 percent on SNMP devices with a profile), agent-ingest
   p95 unchanged while the normalise task runs, zero change events caused by the upgrade itself,
   the real v4 to v6 role reconcile, and a browser pass of the Radix menus (Devices facets and
   the Inventory selects) on an appliance-like backend. A host-only VMware reference appliance,
   reached through the Windows workstation, is the place; `.221` is never the acceptance
   target.
