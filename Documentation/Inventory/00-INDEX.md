# Inventory & Compliance — Design Set Index

*Status: designed 2026-10-07 (verified against `main` @ 482d023, release 1.23.33, highest migration 153). Phase 0 is merged (PR #40, `main` @ 139363b) and Phase 1, the Inventory dashboard, is built on branch `claude/inventory-phase1-dashboard` and has been through an adversarial review and its fix round; both are unreleased and ship together in one release that is planned (not yet merged, tagged or published). Phase 0 plus Phase 1 carry migrations 157, 158 and 159. Phases 2 to 7 are not started: the owner paused the roadmap after Phase 1 on 2026-10-09. Updated 2026-10-09 after the fix round and its re-check: every statement below was checked against the code of that date.*
*This index is the navigation hub and the single source of pinned names for the Inventory & Compliance module. Where a sibling doc conflicts with this file, this file wins. For phase status, `04-ROADMAP-PHASES-AND-TASKS.md` wins. For the shape of every Phase 1 API response, `05-P1-API-CONTRACT.md` wins over the sketches in `02`.*

---

## Thesis

ZenPlus already collects most of what an asset inventory needs, but it never brings it together in one place:

- **Servers:** the Windows agent reports OS name, version and build, the installed-software list, CPU, RAM and disks.
- **Network devices:** the poller stores vendor, model, OS version, firmware and serial, and walks ENTITY-MIB modules.

Before this module, none of it could be viewed across the fleet. There was no endpoint that counted software titles or OS releases, the Devices list had no vendor, model or OS version column, and software history was deleted on every agent upload, so nobody could answer "what changed on this server last Tuesday?".

**Inventory & Compliance** turns this scattered data into one trusted, drillable inventory:

- one dashboard covering every OS, firmware, software title and hardware model across servers and network devices;
- every count of machines is clickable down to the hosts behind it, and the list has exactly that many rows (figures that are not a count of machines, such as the components in a chassis or a product's *Behind latest*, are plain text);
- every value carries its data age.

Later phases build compliance on top of that inventory: lifecycle (EOL), software policies, frameworks and vulnerability correlation. The rule throughout is to normalise once and reuse everywhere. The August 2026 CVE/EOL design set (`Documentation/Compliance/`) becomes the last phase. It plugs into this inventory instead of building its own identity layer.

**The single most important early action: start recording change history now.** History can't be backfilled. Project rules forbid fabricating evidence, so every week before Phase 0 ships is a week of install/upgrade/firmware history lost for good.

## Reading order

| Doc | Contents | Read when |
|---|---|---|
| `00-INDEX.md` | This file: thesis, pinned names, how this set reconciles with the Compliance set | Always first |
| `01-RESEARCH-AND-GAP-ANALYSIS.md` | Market research (13 products, standards, data sources), what ZenPlus has today (file:line), gap list | Product framing, before designing |
| `02-ARCHITECTURE-DATA-MODEL-API.md` | Asset model, normalisation engine, change capture, snapshots, DDL, API sketch, RBAC, scoping, performance budgets, reports | Building any backend piece. For the endpoints themselves, read `05` |
| `03-UI-UX-SPEC.md` | Sidebar, routes, every page (as built in P1, planned after), drill-down rules, cross-links into existing pages, states, accessibility, the shipped components | Building or changing the dashboard |
| `04-ROADMAP-PHASES-AND-TASKS.md` | **The status board and how to resume** (first section), phases P0–P7, task tables with estimates, **what was built** per phase with its verification and measured numbers, exit criteria, test plan, release plan, risks, KPIs, owner decisions | Planning and execution; the status of record |
| `05-P1-API-CONTRACT.md` | **The authoritative contract of the Phase 1 read API**: conventions, the asset filter vocabulary, every endpoint with real examples cut from the golden fixtures, the drill-down map, the definitions of health and freshness, measured performance | Calling or changing any `/api/v1/inventory` endpoint; building a page |
| `06-RELEASE-NOTES-DRAFT.md` | Draft release notes for the release that ships Phase 0 and Phase 1 together: migrations 157 to 159, roles, the first hour after the upgrade, how to go back, the operator checks, known limitations and the support section | Cutting the release |
| `07-PHASE1-FOLLOWUPS.md` | Every finding of the Phase 1 review and re-check that is still open: severity, why it is not in this release, and the phase that should take it | Before starting P2, or when someone asks "was that known?" |

The customer-facing knowledge-base articles are the sources in `inventory-kb/` at the repository root (see its `README.md`); they are published to `https://zentryc.com/kb/zenplus/inventory` separately, with the release.

A clickable UI prototype and plan view, built with example data, accompanies this set: https://claude.ai/artifact/27pcoTdNjochBv1sy7FNcG (private; the owner shares it from the page's Share menu). In the prototype, every Overview, Lifecycle and policy number opens a list of exactly that size, which demonstrates the drill-down contract in doc 03 §2. The prototype shows design intent and example data only; where it differs from the shipped screens (the Lifecycle and Compliance rows, for instance, are not in this release), the documents here win.

## Status

The status of record, with how to resume the work, is the first section of `04-ROADMAP-PHASES-AND-TASKS.md`. In short (2026-10-09):

| Phase | State |
|---|---|
| P0 Foundations and change capture | Built and merged to `main` (PR #40); ships with P1 |
| P1 Inventory dashboard MVP | Built, reviewed, re-checked and fixed; closed by the owner |
| The release carrying P0 and P1 (migrations 157, 158, 159; 1.24.0 proposed) | **Planned**: not yet merged, tagged or published; reference-appliance acceptance and the canary stage are open |
| P2 to P7 | Not started; paused by the owner on 2026-10-09 |

## Pinned names registry (authoritative)

### Product and navigation

| Item | Pinned value |
|---|---|
| Module name | **Inventory & Compliance** |
| Sidebar group | `id: 'inventory'`, label **"Inventory & Compliance"**, short **"Inventory"** (shown under the icon of the collapsed sidebar), icon `PackageSearch`; placed after the `servers` group, before `apm` |
| Inventory routes | `/inventory` (Overview) · `/inventory/assets` (`?asset=<kind>:<id>` opens the flyout) · `/inventory/os` (`?kind=`) · `/inventory/software` · `/inventory/software/:productKey` (`?tab=versions\|installed\|timeline\|evidence`) · `/inventory/hardware` (`?vendor=`) · `/inventory/changes` · `/inventory/lifecycle` (P4) · `/inventory/settings`. **Shipped in P1: every route except `/inventory/lifecycle`.** Changes moved from P2 to P1, and Settings ships as a minimal page (the *Data & retention* tab only) |
| URL vocabulary | The address of every list page carries its filters under **the same names as the API query parameters** of the matching endpoint (scope decision S6; the asset filter vocabulary is section 2 of `05`, implemented in `dashboard/src/pages/inventory/filters.ts`). A drill link and an API call differ only by the path |
| Compliance routes (P5+) | `/compliance` (Posture) · `/compliance/policies` · `/compliance/exceptions` · `/compliance/frameworks` · `/compliance/vulnerabilities` (P6) · `/compliance/vulnerabilities/:cveId` (P6) · `/compliance/patches` (P6) · `/compliance/settings` |
| Page folders | `dashboard/src/pages/inventory/` (`InventoryLayout.tsx`, `api.ts`, `types.ts`, `filters.ts` for the filter vocabulary, `urlState.ts` and `useInventoryUrlState.ts` for URL state, one `Inventory<Name>Page.tsx` per route) · `dashboard/src/components/inventory/` (shared components and the pure models the tests exercise) · `dashboard/src/pages/compliance/` (P5+) |
| React Query keys | `['inventory', …]` · `['compliance', …]` |
| KB base | `https://zentryc.com/kb/zenplus/inventory` (i-icon per tab, UDT `KbLink` pattern). The article keys are `KB_ARTICLES` in `dashboard/src/components/inventory/InventoryKbLink.tsx`: `overview`, `assets`, `operating-systems`, `software`, `hardware`, `changes`, `settings` (one per screen) and `getting-started`, `health-and-freshness`, `permissions`, `export`, `troubleshooting`. Sources: `inventory-kb/templates/` |
| Existing-page touchpoints | Devices list gains optional Vendor / Model / OS version columns (off by default) and Vendor and OS version filters. Device detail Inventory card links its operating system or firmware to the matching Inventory list. Server detail System → Software links each package to its catalog product and shows the truncated-list banner. Server Overview OS tile links to Inventory. Servers → "Inventory" nav row is relabelled **"All Servers"** (route unchanged). All of these are built in P1 (task 1.9; what differs from this list is in `03` section 4) |

### Permissions (RBAC)

New catalog module `("inventory", "Inventory", […])` in `server/app/core/permissions.py`:

| Permission | Grants | Risk | Built-in roles |
|---|---|---|---|
| `inventory.view` | Every Inventory page and read endpoint, within the user's tag scope; Inventory Settings read-only | standard | operator, viewer, read_only, **network_team, systems_team, security_team** (since definition version 6) |
| `inventory.export` | CSV export of inventory datasets (serials and software lists are reconnaissance data) | **sensitive** | operator |
| `inventory.manage` | Changing the module settings in P1 (the page itself opens with `inventory.view`, read-only); catalog rules, categories, asset attributes, lifecycle overrides and shared saved views are planned for later phases | standard | operator |

Compliance permissions (P5+): keep `compliance.view`, `compliance.triage` and `compliance.manage` as pinned in the Compliance set, and add **`compliance.approve`** (approve or revoke exceptions; separation of duties; risk `privileged`; admin only by default).

Wiring:

- `PERMISSION_IMPLIES` gains `inventory.manage → inventory.view` and `inventory.export → inventory.view`.
- `BUILTIN_ROLE_DEFINITION_VERSION` goes from 4 to **5** in Phase 0 (4 was taken by the `servers.*` split and the team roles in release 1.23.35) and to **6** in Phase 1. Version 5 added `inventory.*` to operator, viewer and read_only. Version 6 applies owner decision D9 together with the UI: `network_team`, `systems_team` and `security_team` get `inventory.view` only; `application_team` gets nothing; no team role gets `inventory.export` or `inventory.manage`. An appliance still on release 1.23.35 is at version 4 and reaches 6 in one reconcile at the first start of the new release (the reconcile sets the permissions of every built-in role whose stored version is below 6 to the current definition); each built-in role's change is written to the role history (`builtin_reconcile`). **Custom roles are never changed**: an administrator must grant `inventory.view` (and export or manage where needed) to each custom role by hand, which the release notes say. The one-line descriptions: `BUILTIN_ROLE_META` now ends the descriptions of the three team roles with *Includes read access to Inventory (inventory.view)*, but it is used only when a built-in role row is created, so an existing appliance keeps the old text (the permissions are reconciled, the descriptions are not) and `application_team` is unchanged.
- `route_policy._POLICIES["inventory"] = (("inventory.view",), ("inventory.manage",))`.
- Export routes declare `require_permission("inventory.export")` explicitly.
- The role editor (address `/settings/general?tab=users&sub=roles`, reached as *Settings > Users & Access > Roles & Permissions*; the permission step is *Access*) renders `/roles/catalog`, so the new module row appears with no UI change.

### Backend

| Item | Pinned value |
|---|---|
| Router | `server/app/api/v1/inventory.py`: `APIRouter(prefix="/inventory", tags=["Inventory"])`, mounted in `main.py` with `dependencies=[policy("inventory")]` |
| Services | `services/inventory_normalize.py` (OS, firmware, vendor and software normalisation) · `services/versioning.py` (scheme-aware version comparators shared with baselines and compliance) · `services/inventory_query.py` (asset union, filters, facets, summary, OS, hardware, changes, flyout) · `services/inventory_software_query.py` (software catalog, publishers, product page, installations) · `services/inventory_trends_settings.py` (trends and the settings blob) · `services/inventory_export.py` (CSV) · `services/inventory_service.py` (normalise loop, software-key backfill, daily snapshot and retention; a later split into `inventory_snapshots.py` is a pure file move) · `schemas/inventory.py` |
| Background loops | `inventory_normalize_loop` (45 s after start, then 2 min after each run; normalises dirty or outdated identity rows in batches of 500 and software rows in batches of 2,000, at most 100,000 software rows per run, including the post-upgrade backfill) · `inventory_snapshot_loop` (wakes every 5 min, writes daily at 15 min past `snapshot_hour`, default 00:15 appliance time, held until the process has been up 30 min and the backlog is empty, plus retention purge) · `catalog_warmer_loop` (`inventory_software_query`; keeps the whole-estate software tables warm, off with `ZENPLUS_INVENTORY_CATALOG_WARMER=0`) |
| Advisory lock keys | **1515074397** (normalise) · **1515074398** (snapshot). The Compliance set reserved …395/…396; re-verify all four are unused at implementation |
| Settings storage | `system_settings` key **`'inventory'`** (JSON: freshness thresholds, change retention days, snapshot hour, noise-category defaults, trends switch) · key **`'inventory.capture_started_at'`** (JSON string, UTC, written once by migration 158: when change capture began) |
| Built-in content | `server/app/data/inventory/software_rules.json` · `vendor_aliases.json` · `os_patterns.json` (versioned packs; local overrides live in DB tables) |

### Postgres tables and columns

| Object | Phase | Role |
|---|---|---|
| `inventory_identity` | P0 | **One normalised identity row per asset** (`asset_kind, asset_id`): vendor, os_family/product/release/version/build/edition, hw vendor/model/serial, each with its source; dirty-marked by triggers (`norm_rev` NULL), filled by the normalise loop and by ingest; `values_rev` records the rule-pack revision the values were computed under, so a revision change never writes history. Raw columns are never overwritten |
| `devices` + `sys_descr`, `identity_updated_at` | P0 | Raw sysDescr evidence; time of the last successful system-info poll (freshness) |
| `servers` + `inventory_updated_at`, `inventory_baselined_at`, `inventory_collected_at`, `software_truncated`, `hw_manufacturer`, `hw_model`, `hw_serial`, `bios_version`, `bios_date`, `virtualization` | P0 (the hardware columns are empty until the agent reports them, planned for P3) | Freshness (migration 158 seeds `inventory_updated_at` from the newest software row of a baselined server), first-snapshot gate for change capture, newest accepted snapshot time (ordering guard), truncated-list flag, raw hardware evidence |
| `server_software_inventory` + `architecture`, `source`, `install_location`, `first_seen_at`, `name_key`, `publisher_key`, `category`, `norm_rev` | P0 (`first_seen_at` repaired by 158) | Keep evidence that is dropped today, plus normalised grouping keys. `first_seen_at` is the time Inventory first saw the row; for rows that existed at the upgrade it is the install date the machine reported, or the migration time, and the pages show it as *before <capture date>* |
| `inventory_change_events` | P0 | Append-only install / remove / version-change / firmware / hardware history, written by triggers |
| `inventory_daily_snapshots` | P0 | One row per day per metric family: trend charts and point-in-time audit evidence |
| `inventory_software_products` | P2 | Normalised catalog: publisher, product, category, flags, CPE vendor:product, lifecycle slug |
| `inventory_software_rules` | P2 | Local normalisation rules (raw name/publisher pattern → product), priority ordered, overriding the built-in pack |
| `inventory_publishers` | P2 | Publisher alias → canonical publisher |
| `inventory_asset_meta` | P2 | Owner, department, criticality 1–5, lifecycle state, purchase/warranty dates, notes, exclusion flag (**supersedes** the Compliance set's `compliance_asset_meta`) |
| `inventory_saved_views` | P2 | Personal or shared saved filters per page |
| `server_patch_inventory` | P3 | Installed Windows KBs/hotfixes (the Compliance set's pinned name, re-homed) |
| `eol_definitions` | P4 | Lifecycle cycles per product (endoflife.date snapshot + feed). Pinned name kept from the Compliance set |
| `inventory_lifecycle_overrides` | P4 | Manual EOL/EOS dates for products with no feed coverage, and for hardware models |
| `compliance_policies` / `compliance_policy_rules` / `compliance_results` / `compliance_exceptions` / `compliance_snapshots` / `compliance_framework_controls` | P5 | Unified policy engine (spec in doc 02 §10) |

### Migrations

Numbered in ship order. **157, 158 and 159 are the numbers used by Phase 0 and Phase 1**: `main` already holds 154 (role scope and server permissions) and 156 (report templates), 155 is intentionally unused, and the Compliance set's original 080 to 082 were taken long ago. Re-check `scripts/migrations.lock` on `origin/main` before each phase and before the pull request, because parallel branches take numbers (it ended at 157 on `main` when the Phase 1 fix round started).

| File | Phase | Contents |
|---|---|---|
| `migrate-157-inventory-foundation.sql` | P0 | `inventory_identity` + dirty-marking triggers; evidence columns on `devices`/`servers`/`server_software_inventory`; `inventory_change_events` + capture triggers; `inventory_daily_snapshots`; baselined-at seed for existing servers; indexes; GRANTs |
| `migrate-158-inventory-phase1-followups.sql` | P1 (fix round) | The capture start (`system_settings['inventory.capture_started_at']`, from the ledger time of 157); `servers.inventory_updated_at` seeded from the newest software row of every baselined server that has none; `first_seen_at` of software that pre-dates the capture repaired from the install date; partial indexes `idx_ssi_endpoint_protection`, `idx_ssvc_endpoint_protection` and `idx_servers_device_id`. No table changes shape |
| `migrate-159-audit-actor-role-width.sql` | P1 (fix round) | `audit_logs.actor_role` widened from `VARCHAR(20)` to `VARCHAR(50)` so that the audit entries of export and settings changes can hold any role name |

Planned for later phases. The numbers are not decided and no file name is pinned: each phase takes the next free number in `scripts/migrations.lock` when it writes its file.

| Migration | Phase | Planned contents |
|---|---|---|
| migration N+1 (planned) | P2 | `inventory_software_products`, `inventory_software_rules`, `inventory_publishers`, `inventory_asset_meta`, `inventory_saved_views` |
| migration N+2 (planned) | P3 | `server_patch_inventory`; `server_software_inventory` key widened to `(server_id, source, package_name, architecture)` |
| migration N+3 (planned) | P4 | `eol_definitions`, `inventory_lifecycle_overrides`; alert-metric constraint supersede for `inventory_eol_*` |
| migration N+4 (planned) | P5 | Policy engine tables; baseline → policy data copy (baseline tables left in place, read-only, for rollback) |
| migrations N+5 and later (planned) | P6 | The Compliance set's `migrate-081/082` contents, renumbered |

The earlier drafts of this table named the planned files `migrate-155-…` to `migrate-158-…` (P2 to P5) and `migrate-159+` (P6). Those numbers collide with files that exist (156 is the report templates, 157 to 159 are Inventory's) or that must stay unused (155), so they are no longer written as file names anywhere in this set. To confirm the state of the tree: `ls scripts/migrate-15*` and the tail of `scripts/migrations.lock`.

## Reconciliation with the Compliance design set (`Documentation/Compliance/`, 2026-08-18)

The Compliance set is unbuilt and still valid as the vulnerability engine. These amendments apply. The Compliance `00-INDEX.md` carries a pointer to this section.

| Compliance-set pin | Amended to | Why |
|---|---|---|
| Sidebar group "Compliance" (short "Comply") | Items inside the **"Inventory & Compliance"** group, under a "Compliance" node with children | One module, one group. Inventory is the foundation compliance reads from |
| `/compliance/eol` | **`/inventory/lifecycle`** (old path redirects) | Lifecycle is an inventory attribute, and it ships in P4, before any compliance page |
| `/compliance/software` | **`/inventory/software`** gains risk columns when the CVE feed is active | Avoid two software catalog pages (AGENTS.md: no duplicate pages) |
| `/compliance/assets` | `/inventory/assets` with a risk column and sort | Same reason |
| `compliance_asset_meta` | **`inventory_asset_meta`** | Owner and criticality are inventory facts that compliance consumes |
| `compliance_product_aliases` | **`inventory_software_products` + `inventory_software_rules`** | A single normalisation layer |
| `compliance_oid_dictionary` | Built-in `os_patterns.json` + `inventory_identity.os_family` (P0) | Same data, shipped earlier |
| CV-E0 columns `devices.os_family`, `servers.distro_*` | `inventory_identity.os_family / os_product / os_release` | One normalised layer for every asset kind |
| CV-E0 tasks 0.1–0.5 (identity hardening) | **Absorbed into P0 and P3** of this set | They are inventory work |
| Migrations 080/081/082 | 157 and later (numbers taken since August) | Lockfile reality |

Everything else in the Compliance set (feed protocol, matching kinds, confidence tiers, FRS/ARS scoring, SLA policy, triage states) stands as written and becomes Phase P6.

## Conflict-resolution rules

1. Pinned names here win over any sibling doc and over the Compliance set where the table above amends it.
2. Phase status: doc 04 wins.
3. Project rules (`AGENTS.md`) are binding and override this set:
   - migrations are append-only, forward-only and lockfiled;
   - integration tests use disposable fixtures, never installed databases;
   - all five required CI checks stay;
   - signing verification is never bypassed.
