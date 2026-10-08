# Inventory & Compliance — Design Set Index

*Status: Design proposal · 2026-10-07 · Verified against `main` @ 482d023 (release 1.23.33, highest migration 153).*
*This index is the navigation hub and the single source of pinned names for the Inventory & Compliance module. Where a sibling doc conflicts with this file, this file wins. For phase status, `04-ROADMAP-PHASES-AND-TASKS.md` wins.*

---

## Thesis

ZenPlus already collects most of what an asset inventory needs, but it never brings it together in one place:

- **Servers:** the Windows agent reports OS name, version and build, the installed-software list, CPU, RAM and disks.
- **Network devices:** the poller stores vendor, model, OS version, firmware and serial, and walks ENTITY-MIB modules.

None of it can be viewed across the fleet. There is no endpoint that counts software titles or OS releases. The Devices list doesn't show vendor, model or OS version at all. Software history is deleted on every agent upload, so nobody can answer "what changed on this server last Tuesday?".

**Inventory & Compliance** turns this scattered data into one trusted, drillable inventory:

- one dashboard covering every OS, firmware, software title and hardware model across servers and network devices;
- every number is clickable down to the hosts behind it;
- every value carries its data age.

Later phases build compliance on top of that inventory: lifecycle (EOL), software policies, frameworks and vulnerability correlation. The rule throughout is to normalise once and reuse everywhere. The August 2026 CVE/EOL design set (`Documentation/Compliance/`) becomes the last phase. It plugs into this inventory instead of building its own identity layer.

**The single most important early action: start recording change history now.** History can't be backfilled. Project rules forbid fabricating evidence, so every week before Phase 0 ships is a week of install/upgrade/firmware history lost for good.

## Reading order

| Doc | Contents | Read when |
|---|---|---|
| `00-INDEX.md` | This file: thesis, pinned names, how this set reconciles with the Compliance set | Always first |
| `01-RESEARCH-AND-GAP-ANALYSIS.md` | Market research (13 products, standards, data sources), what ZenPlus has today (file:line), gap list | Product framing, before designing |
| `02-ARCHITECTURE-DATA-MODEL-API.md` | Asset model, normalisation engine, change capture, snapshots, DDL, API contract, RBAC, scoping, performance, reports | Building any backend piece |
| `03-UI-UX-SPEC.md` | Sidebar, routes, every page with wireframes, drill-down rules, cross-links into existing pages, states, accessibility | Building the dashboard |
| `04-ROADMAP-PHASES-AND-TASKS.md` | Phases P0–P7, task tables with estimates, exit criteria, test plan, release plan, risks, KPIs, owner decisions | Planning and execution |

A clickable UI prototype and plan view, built with example data, accompanies this set: https://claude.ai/artifact/27pcoTdNjochBv1sy7FNcG (private; the owner shares it from the page's Share menu). In the prototype, every Overview, Lifecycle and policy number opens a list of exactly that size, which demonstrates the drill-down contract in doc 03 §2.

## Pinned names registry (authoritative)

### Product and navigation

| Item | Pinned value |
|---|---|
| Module name | **Inventory & Compliance** |
| Sidebar group | `id: 'inventory'`, label **"Inventory & Compliance"**, short **"Assets"**, icon `PackageSearch`; placed after the `servers` group, before `apm` |
| Inventory routes | `/inventory` (Overview) · `/inventory/assets` · `/inventory/os` · `/inventory/software` · `/inventory/software/:productKey` · `/inventory/hardware` · `/inventory/changes` · `/inventory/lifecycle` (P4) · `/inventory/settings` |
| Compliance routes (P5+) | `/compliance` (Posture) · `/compliance/policies` · `/compliance/exceptions` · `/compliance/frameworks` · `/compliance/vulnerabilities` (P6) · `/compliance/vulnerabilities/:cveId` (P6) · `/compliance/patches` (P6) · `/compliance/settings` |
| Page folders | `dashboard/src/pages/inventory/` (`InventoryLayout.tsx`, `api.ts`, `types.ts`, `helpers.tsx`, one file per page) · `dashboard/src/pages/compliance/` (P5+) |
| React Query keys | `['inventory', …]` · `['compliance', …]` |
| KB base | `https://zentryc.com/kb/zenplus/inventory` (i-icon per tab, UDT `KbLink` pattern) |
| Existing-page touchpoints | Devices list gains Vendor / Model / OS version columns. Device detail Inventory card links firmware to `/inventory/os`. Server detail System → Software links each package to its catalog product. Servers → "Inventory" nav row is relabelled **"All Servers"** (route unchanged). |

### Permissions (RBAC)

New catalog module `("inventory", "Inventory", […])` in `server/app/core/permissions.py`:

| Permission | Grants | Risk | Built-in roles |
|---|---|---|---|
| `inventory.view` | Every Inventory page and read endpoint, within the user's tag scope | standard | operator, viewer, read_only |
| `inventory.export` | CSV export of inventory datasets (serials and software lists are reconnaissance data) | **sensitive** | operator |
| `inventory.manage` | Catalog rules, categories, asset attributes, lifecycle overrides, shared saved views, module settings | standard | operator |

Compliance permissions (P5+): keep `compliance.view`, `compliance.triage` and `compliance.manage` as pinned in the Compliance set, and add **`compliance.approve`** (approve or revoke exceptions; separation of duties; risk `privileged`; admin only by default).

Wiring:

- `PERMISSION_IMPLIES` gains `inventory.manage → inventory.view` and `inventory.export → inventory.view`.
- `BUILTIN_ROLE_DEFINITION_VERSION` goes from 4 to **5** (4 was taken by the `servers.*` split and the team roles in release 1.23.35). Custom roles are never changed. The four team roles (`network_team`, `systems_team`, `application_team`, `security_team`) do **not** get `inventory.*` in Phase 0: nothing is visible yet, and which team sees what is an owner decision (D9 in `04-ROADMAP-PHASES-AND-TASKS.md`) applied together with the UI in P1.
- `route_policy._POLICIES["inventory"] = (("inventory.view",), ("inventory.manage",))`.
- Export routes declare `require_permission("inventory.export")` explicitly.
- The role editor at `/settings/general?tab=users&sub=roles` renders `/roles/catalog`, so the new module row appears with no UI change.

### Backend

| Item | Pinned value |
|---|---|
| Router | `server/app/api/v1/inventory.py`: `APIRouter(prefix="/inventory", tags=["Inventory"])`, mounted in `main.py` with `dependencies=[policy("inventory")]` |
| Services | `services/inventory_normalize.py` (OS, firmware, vendor and software normalisation) · `services/versioning.py` (scheme-aware version comparators shared with baselines and compliance) · `services/inventory_query.py` (asset union, facets, aggregates) · `services/inventory_service.py` (normalise loop, software-key backfill, daily snapshot and retention; a later split into `inventory_snapshots.py` is a pure file move) |
| Background loops | `inventory_normalize_loop` (every 2 min; normalises dirty or outdated identity and software rows in batches of 500, including the post-upgrade backfill) · `inventory_snapshot_loop` (daily 00:15 appliance time, plus retention purge) |
| Advisory lock keys | **1515074397** (normalise) · **1515074398** (snapshot). The Compliance set reserved …395/…396; re-verify all four are unused at implementation |
| Settings storage | `system_settings` key **`'inventory'`** (JSON: freshness thresholds, change retention days, snapshot hour, noise-category defaults) |
| Built-in content | `server/app/data/inventory/software_rules.json` · `vendor_aliases.json` · `os_patterns.json` (versioned packs; local overrides live in DB tables) |

### Postgres tables and columns

| Object | Phase | Role |
|---|---|---|
| `inventory_identity` | P0 | **One normalised identity row per asset** (`asset_kind, asset_id`): vendor, os_family/product/release/version/build/edition, hw vendor/model/serial, each with its source; dirty-marked by triggers (`norm_rev` NULL), filled by the normalise loop and by ingest; `values_rev` records the rule-pack revision the values were computed under, so a revision change never writes history. Raw columns are never overwritten |
| `devices` + `sys_descr`, `identity_updated_at` | P0 | Raw sysDescr evidence; time of the last successful system-info poll (freshness) |
| `servers` + `inventory_updated_at`, `inventory_baselined_at`, `inventory_collected_at`, `software_truncated`, `hw_manufacturer`, `hw_model`, `hw_serial`, `bios_version`, `bios_date`, `virtualization` | P0 (hardware filled in P3) | Freshness, first-snapshot gate for change capture, newest accepted snapshot time (ordering guard), truncated-list flag, raw hardware evidence |
| `server_software_inventory` + `architecture`, `source`, `install_location`, `first_seen_at`, `name_key`, `publisher_key`, `category`, `norm_rev` | P0 | Keep evidence that is dropped today, plus normalised grouping keys |
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

Numbered in ship order. **157 is the number used by P0**: `main` already holds 154 (role scope and server permissions) and 156 (report templates), and the Compliance set's original 080 to 082 were taken long ago. Re-check `scripts/migrations.lock` before each phase, because parallel branches take numbers.

| File | Phase | Contents |
|---|---|---|
| `migrate-157-inventory-foundation.sql` | P0 | `inventory_identity` + dirty-marking triggers; evidence columns on `devices`/`servers`/`server_software_inventory`; `inventory_change_events` + capture triggers; `inventory_daily_snapshots`; baselined-at seed for existing servers; indexes; GRANTs |
| `migrate-155-inventory-catalog.sql` | P2 | `inventory_software_products`, `inventory_software_rules`, `inventory_publishers`, `inventory_asset_meta`, `inventory_saved_views` |
| `migrate-156-inventory-coverage.sql` | P3 | `server_patch_inventory`; `server_software_inventory` key widened to `(server_id, source, package_name, architecture)` |
| `migrate-157-inventory-lifecycle.sql` | P4 | `eol_definitions`, `inventory_lifecycle_overrides`; alert-metric constraint supersede for `inventory_eol_*` |
| `migrate-158-compliance-policies.sql` | P5 | Policy engine tables; baseline → policy data copy (baseline tables left in place, read-only, for rollback) |
| `migrate-159+` | P6 | The Compliance set's `migrate-081/082` contents, renumbered |

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
