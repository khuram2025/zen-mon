# 04 — Roadmap, Phases, Tasks & Test Plan

*Status: Design proposal · 2026-10-07. This doc is the implementation status of record.*

*Estimates are in engineering days for one engineer working with Claude Code. They include tests and docs but not review wait time.*

*Every phase ships through the normal workflow:*
- *a branch, then a PR with all five required CI checks (server, dashboard, poller, agent-ubuntu, agent-windows);*
- *migrations appended to `scripts/migrations.lock`;*
- *a signed OTA release through `zentryc-release`;*
- *acceptance on the reference appliance.*

*Integration tests use disposable Postgres/ClickHouse fixtures, never `/opt/zenplus` databases.*

---

## 1. Phase map

```
 P0 ───────► P1 ───────► P2 ───────► P3 ───────► P4 ───────► P5 ───────► P6 ──► P7
 Foundations Inventory   Catalog,    Coverage    Lifecycle   Compliance  CVE     Backlog
 & capture   dashboard   history UI, expansion   (EOL/EOS)   policies &  corre-
 (dark)      MVP         trends,     (agent,     intelli-    frameworks  lation
             ◄ the ask   reports     Linux, KB)  gence
 ~3 wk       ~5 wk       ~3.5 wk     ~4.5 wk     ~3 wk       ~4.5 wk     Compliance set
```

**Critical path:** P0 → P1.

- P2 and P3 can overlap: P3's agent work lives in the `ZenPlus_Agent` repo.
- P4 needs only P0 and P1.
- P5 needs P2 (catalog + asset meta) and P4 (lifecycle).
- P6 needs P2 and P4.

**The one rule that sets the order:** P0 ships first, as soon as possible, inside a normal release and without UI. Change history can't be backfilled, so every day earlier is a day of evidence kept.

---

## 2. Phases

### P0 — Foundations & change capture (dark ship, no UI) · ≈ 14 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 0.1 | `migrate-154-inventory-foundation.sql` (doc 02 §3.2, §3.3, §5, §6): `inventory_identity`, evidence columns, `inventory_change_events` with triggers and suppression GUC, `inventory_daily_snapshots`, baselined-at seed, indexes, GRANTs. Plus lockfile entry and `lint-migrations` | db | 2 | — |
| 0.2 | `services/versioning.py` (comparators: win_build, semverish, fortios, panos, cisco_ios/iosxe, junos) and `services/inventory_normalize.py` (OS, network OS, vendor, software name). Rule packs `software_rules.json`, `vendor_aliases.json`, `os_patterns.json`. Golden fixtures cover every row of the tables in doc 02 §4 plus the live values seen on .221 | api | 4 | — |
| 0.3 | Ingest hardening (`host_metric_service._upsert_inventory`): keep architecture, source and install_location; inline keys and category; same-name dedupe; truncation guard (no prune at ≥ 1,000); stamp `inventory_updated_at` / `inventory_baselined_at`; normalise the server identity inline | api | 1.5 | 0.1, 0.2 |
| 0.4 | Poller (Go): persist `sys_descr`; stamp `identity_updated_at` on every successful system-info poll. Profiles: `os_family` hint on every built-in profile. Verify the PAN/F5 template-value precedence end to end (no Go change needed for it) | poller, content | 2 | 0.1 |
| 0.5 | `inventory_normalize_loop` (2 min, lock 1515074397, batches of 500, `asyncio.to_thread`, suppression on re-normalise) and `inventory_snapshot_loop` (daily 00:15, lock 1515074398, retention purge). Registered in `main.py` startup and shutdown lists | api | 1.5 | 0.2 |
| 0.6 | RBAC: `inventory` module in `PERMISSION_MODULES`; `_OPERATOR`/`_VIEWER`/`_READ_ONLY` lists; `PERMISSION_IMPLIES`; `PERMISSION_RISK["inventory.export"]="sensitive"`; `BUILTIN_ROLE_DEFINITION_VERSION = 4`; `_POLICIES["inventory"]`; `test_permissions_model.py` and `integration/test_access_control_e2e.py` `READ_ONLY_PERMS` updated | api | 1 | — |
| 0.7 | Integration tests on a disposable Postgres fixture: trigger and suppression behaviour (T3–T9 below) | test | 2 | 0.1–0.5 |

**Exit criteria**

- Change events accumulate on the reference appliance with **zero events from the upgrade itself**.
- `inventory_identity` is populated for 100% of assets.
- Identity coverage is measured and reported in the PR. Targets on SNMP devices with a profile: vendor ≥ 95%, OS version ≥ 90% (PAN/F5 now filled from templates), model ≥ 90%.
- Agent-ingest p95 latency is unchanged (± 5%).

### P1 — Inventory dashboard MVP (the request) · ≈ 24 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 1.1 | `services/inventory_query.py`: asset union with server↔device dedupe, filters, facets, health predicates, tag scoping, 60 s summary cache per scope key | api | 3 | P0 |
| 1.2 | `api/v1/inventory.py` read endpoints: summary, assets, facets, asset flyout, os, software, publishers, product, installations, hardware, changes (read), settings GET/PUT. `GET /inventory/export` with CSV streaming, injection guard and audit. Mounted with `policy("inventory")` | api | 4 | 1.1 |
| 1.3 | UI shell: nav group (doc 03 §1.1; P1 rows only), routes, `InventoryAccessGate`, `InventoryLayout`, `InventoryMetaLine`, `components/inventory/*` primitives, `requiredRoutes` update, **nav-target validation test** in the dashboard CI job | ui | 2 | — |
| 1.4 | Overview page: KPIs, OS panel, network firmware panel, top software, version sprawl, hardware, recent changes, inventory health, security-agent coverage, empty/onboarding states | ui | 2.5 | 1.2, 1.3 |
| 1.5 | Assets page: facets, server-side table, column chooser, flyout | ui | 3 | 1.2, 1.3 |
| 1.6 | Operating Systems page: tree table, kind switch, build-currency card | ui | 1.5 | 1.2 |
| 1.7 | Software catalog and product page (Versions, Installed on, Evidence tabs; Timeline tab placeholder until P2) | ui | 3 | 1.2 |
| 1.8 | Hardware page (network vendors/models, models × firmware matrix, modules, server CPU/RAM/disk buckets) | ui | 1.5 | 1.2 |
| 1.9 | Existing-page touchpoints: Devices list Vendor/Model/OS columns and filters (+ `GET /devices` vendor/os params), device firmware link, server Software links and truncated banner, Server Overview OS tile link, "All Servers" relabel | ui+api | 1.5 | 1.2 |
| 1.10 | Tests: `test_inventory_route_guards.py`, scope tests (T10–T12), export injection (T13), perf fixture (T15), dashboard drill-down equality script (T-UI2), strict build, smoke | test | 2 | 1.1–1.9 |
| 1.11 | KB articles (`zentryc.com/kb/zenplus/inventory`, one per tab), release notes (announce the new permissions and that **custom roles need `inventory.view`**), `.env.example` unchanged (no new secrets) | docs | 1 | — |

**Exit criteria**

- Each of these questions takes ≤ 3 clicks from the Overview, and the drilled list's total equals the clicked number:
  - "How many servers run Windows Server 2019, and which builds?"
  - "Which hosts run Chrome older than 150?"
  - "Which FortiGates run 7.2.x?"
  - "Which devices are missing a serial?"
- A scoped user sees only in-scope assets on every endpoint (T10–T12).
- An export of 10k rows completes in < 5 s and is audit-logged.
- All five CI checks are green, and acceptance passes on the reference appliance.

### P2 — Catalog normalisation, history UI, trends, asset attributes, reports · ≈ 16 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 2.1 | `migrate-155-inventory-catalog.sql`. Catalog service: rule precedence, revision bump and re-key, preview, unmatched queue, "report inaccuracy" pre-fill | db+api | 3 | P1 |
| 2.2 | Settings → Software catalog UI: rules with preview, products, publishers, unmatched queue | ui | 3 | 2.1 |
| 2.3 | Changes page; product Timeline tab; change mini-lists on device/server pages | ui | 2.5 | P1 |
| 2.4 | `GET /inventory/trends` (scope-refused) and Overview sparklines and trend charts | api+ui | 1.5 | P0 snapshots |
| 2.5 | Asset meta API and flyout editing (owner, department, business service, criticality, lifecycle state, purchase/warranty, mark reviewed), with facets and Assets columns | api+ui | 2 | 2.1 |
| 2.6 | Saved views (personal and shared) on Assets, Software and Changes | api+ui | 1.5 | 2.1 |
| 2.7 | Report sections (`inventory_overview`, `_os_distribution`, `_software_summary`, `_hardware_models`, `_changes`), extended `inventory` preset, new `inventory_audit` preset, `_REPORT_TYPES`, schedule smoke test | api+ui | 2.5 | P1 |

**Exit criteria**

- Unmatched software is below 5% of installations on the reference fleet.
- A local rule fix regroups titles within one loop cycle, with zero change events.
- A weekly inventory PDF is delivered by email on schedule.

### P3 — Coverage expansion · ≈ 21 d (the agent tasks run in the `ZenPlus_Agent` repo in parallel)

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 3.1 | Agent `hardware_identity_v1` capability: Win32_ComputerSystem, Win32_ComputerSystemProduct and Win32_BIOS (manufacturer, model, serial, BIOS version/date), virtualisation detection, TPM and Secure Boot state. Ingest into the `servers.hw_*` columns. Gated by `_agent_supports` | agent+api | 3 | P0 |
| 3.2 | Agent `patch_inventory_v1`: QFE/Get-HotFix plus the Windows Update history (KB, title, installed_on). `server_patch_inventory`. Server-page "Updates" sub-tab and a fleet "Updates" view under Software | agent+api+ui | 4 | P0 |
| 3.3 | Agent: lift the 1,000-entry cap (raise to 5,000 and set an explicit `truncated` flag in the batch). Server honours the flag | agent+api | 1 | — |
| 3.4 | **Linux agentless inventory over SSH**, reusing `ncm_credentials` (the cross-module SSH store): `/etc/os-release`, `dpkg-query -W` / `rpm -qa --qf`, `lscpu`, `/proc/meminfo`, `dmidecode` when permitted. Scheduled every 6 h per server with `collection_mode='ssh'`, bounded concurrency, and results written through the same ingest path (`source='dpkg'/'rpm'`) | api | 6 | P0 |
| 3.5 | `migrate-156-inventory-coverage.sql`: `server_patch_inventory`; widen the `server_software_inventory` key to `(server_id, source, package_name, architecture)` with a data move under the suppression GUC | db | 2 | 3.2 |
| 3.6 | Poller: walk `entPhysicalSoftwareRev` and `entPhysicalMfgName` (OIDs already defined at `oids.go:59,61`); module firmware on the Hardware page | poller | 2 | P0 |
| 3.7 | UDT endpoints as asset kind `endpoint` (OUI vendor, type, authorized). KPIs for **unauthorised assets** (CIS 1.2) and **discovered but not monitored** (links to `/discovery/imports`) | api+ui | 3 | P1 |

**Exit criteria**

- Server manufacturer, model and serial are known for ≥ 95% of agent servers.
- At least one Linux server is fully inventoried over SSH on the reference lab.
- The KB list matches `Get-HotFix` exactly on the test VMs.

### P4 — Lifecycle intelligence (EOL/EOS) · ≈ 13.5 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 4.1 | `migrate-157-inventory-lifecycle.sql`: `eol_definitions`, `inventory_lifecycle_overrides`, and an alert-metric constraint supersede (`inventory_eol_reached`, `inventory_eol_within_90d`). The base constraint owner must be re-verified at implementation | db | 1.5 | P1 |
| 4.2 | Release-time builder: fetch the endoflife.date `/products/full` snapshot (MIT, about 3 MB) during the clean build, record its sha256 in the signed release inventory, then load it on upgrade. Mapping from identity/catalog to `(slug, cycle)` via pack rules and endoflife.date `/identifiers` | build+api | 3 | 4.1 |
| 4.3 | Refresh between releases: through the zentryc feed channel (Compliance doc 06) when available, otherwise a signed `.zvb` lifecycle bundle upload. The appliance never calls endoflife.date directly. **Signature verification is mandatory** | api+server | 3 | 4.2 |
| 4.4 | Lifecycle page; stage pills and EOL columns on OS, Software and Assets; Overview EOL KPI; lifecycle colouring of the firmware bars; N / N-1 using feed-latest | ui | 3 | 4.2 |
| 4.5 | Overrides UI (reason, expiry) and hardware EoS/EoL CSV import; optional Cisco EoX API connector when entitled credentials exist (credentials stored with existing credential encryption, never in the repo) | api+ui | 2 | 4.1 |
| 4.6 | Alerts (EOL reached, EOL ≤ 90 d), the `inventory_lifecycle` report section, and the PCI 12.3.4 annual-review report | api | 1 | 4.4 |

**Exit criteria**

- Every OS and firmware release in use on the reference fleet shows a stage or an explicit *Unknown* with an override path.
- Lifecycle works with no internet egress.

### P5 — Compliance policies & frameworks · ≈ 22 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 5.1 | `migrate-158-compliance-policies.sql` and the engine (doc 02 §10.2): rule types, scope resolution, evaluation on ingest, identity change and daily, results with latched `first_failed_at`, snapshots | db+api | 6 | P2, P4 |
| 5.2 | Exceptions: requested → approved (`compliance.approve`, new permission, admin-only default) → expired; reason, owner, expiry; audit trail | api | 2 | 5.1 |
| 5.3 | Baselines → policies migration with a parity test (identical outcomes on the fixture fleet). `/server-baselines` redirect; nav row moves | api+ui | 2 | 5.1 |
| 5.4 | Posture overview, Policies list and editor with "would fail N assets" preview, per-policy results | ui | 5 | 5.1 |
| 5.5 | Exceptions register; Frameworks page (CIS v8.1 1.1/1.2/2.1/2.2/2.3/2.5, NIST CSF 2.0 ID.AM-01/02/08, ISO 27001 A.5.9/A.8.19, PCI DSS 4.0 12.5.1/12.3.4/6.3.3); **audit pack** export (snapshot CSVs + results + exception register + change log, for a date range) | api+ui | 4 | 5.2 |
| 5.6 | Alerts on transitions (dedupe `policy:{rule}:{asset}`), compliance report sections | api | 2 | 5.1 |
| 5.7 | Surface NCM config-compliance on Posture as a summary card linking to the NCM Compliance workspace (no duplicate page) | ui | 1 | 5.4 |

**Exit criteria**

- An auditor walkthrough of CIS 2.1–2.3 on the reference fleet is possible entirely from the UI and the exported audit pack.
- Baseline parity is 100%.

### P6 — Vulnerability correlation

Executes `Documentation/Compliance/07` CV-E1 → CV-E3, with the amendments in `00-INDEX.md` (shared catalog, asset meta, comparators, lifecycle; migrations renumbered from 159).

Sizing is per that document: three M-sized phases, including the zentryc feed builder.

### P7 — Backlog (own specs when scheduled)

- License position (entitlements vs installs) and software metering
- CIS Benchmarks / SCAP-OVAL config hardening (with the NCM config-compliance packs)
- CMDB sync and export (ServiceNow, CSV/REST webhooks) and a public inventory API
- macOS agent inventory
- MSP multi-tenant reporting and branding
- AI copilot grounding ("which upgrade clears the most risk?")

---

## 3. Release plan (proposal)

| Release | Content | Notes |
|---|---|---|
| Next patch (1.23.34) | **P0**, dark | Release notes: new permissions exist; built-in roles reconciled to v4; custom roles unchanged |
| 1.24.0 | **P1** | Headline feature: "Inventory". KB live |
| 1.24.x | **P2** | Catalog, Changes, trends, reports |
| 1.25.0 | **P3 + P4** | Requires the new agent release for 3.1–3.3; appliance-only OTA is valid for the rest (AGENTS.md) |
| 1.26.0 | **P5** | Compliance policies |

Every release:

- tarball permissions normalised (0644/0755, including nested dashboard archives);
- migrations named explicitly in the packager;
- rollback stops binary consumers before the legacy restore;
- signed packages only.

## 4. Test plan

| ID | Type | Case | Expected |
|---|---|---|---|
| T1 | unit | Comparator matrix: `15.2(4)E8 < 15.2(4)E10`, `17.9.4 < 17.9.4a`, `11.1.2-h3 < 11.1.2-h10`, `7.2.5 build1517 < 7.2.8`, `21.4R3-S5.4 > 21.4R3-S5`, `10.0.20348.587 < 10.0.20348.2700`, cross-train refused | Pinned orderings; `None` on cross-train |
| T2 | unit | Normaliser golden fixtures (every row in doc 02 §4, plus live values from .221) | Exact family/product/release/version/edition |
| T3 | integration | First software snapshot of a new server | 0 events; `inventory_baselined_at` set |
| T4 | integration | Second snapshot: 1 added, 1 removed, 1 version change | Exactly 3 events with the correct old/new values |
| T5 | integration | Delete a server that has software rows | 0 `removed` events; prior history retained |
| T6 | integration | Snapshot with ≥ 1,000 entries missing 200 previously seen | No prune; 0 `removed`; truncated health flag |
| T7 | integration | Duplicate same-name entries in one snapshot (HKLM + HKCU) | One row (highest version); 0 flapping events across 3 uploads |
| T8 | integration | Rule-pack revision bump, no source change | All rows re-normalised; **0 events** |
| T9 | integration | FortiGate template `fgt_fw_version` changes 7.2.5 → 7.2.8 | Identity updated within one loop; 1 firmware event |
| T10 | security | Scoped user: summary, assets, facets, os, software, product, installations, hardware, changes, export | Only in-scope assets counted and listed |
| T11 | security | Scoped user requests an out-of-scope asset flyout | 404 |
| T12 | security | Scoped user requests trends or report data | Restricted response / 403, consistent with `reports.py` |
| T13 | security | Export with cells starting `=`, `+`, `-`, `@`, tab | Neutralised; audit log row written |
| T14 | security | Viewer role calls every mutating inventory route | 403 (route-guard test plus live check) |
| T15 | perf | 5k servers × 400 packages, 10k devices, 1M events | Doc 02 §13 budgets met; ingest p95 unchanged while the normalise loop runs |
| T16 | regression | Upgrade N-1 → N and fresh install, with migrations 154+ | Schema gate green both paths; baselined seed correct |
| T17 | regression | Built-in role reconcile v3 → v4 | Built-ins gain inventory perms; custom roles byte-identical |
| T-UI1 | ui | Strict build, `smoke:routes`, nav-target validation | Green; every nav `to:` routed |
| T-UI2 | ui | Drill-down equality: each Overview KPI or chart link → list `total` | Equal for every link on the fixture dataset |
| T-UI3 | ui | User without `inventory.view` | Group hidden; direct URL shows the designed denied state |
| T18 | UAT | Owner answers the four P1 exit questions on the reference appliance | ≤ 3 clicks each, no docs needed |

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Normalisation wrong, so counts can't be trusted | Raw evidence kept and shown; golden fixtures from live data; Unmatched queue; local rules with preview; "report inaccuracy" |
| Trigger overhead on hot write paths (60 s device polls, agent ingest) | `UPDATE OF` column lists plus `WHEN` clauses (no-op updates never enter PL/pgSQL); perf test T15 compares ingest p95 before and after |
| False history (upgrade storms, re-keys, truncation) | Baselined gate, suppression GUC, truncation guard, tests T3–T8; never backfill |
| Double counting (server + its SNMP device row) | Union dedupe on `servers.device_id`; fixture asserts total = distinct machines |
| Scope leaks through aggregates | Scope applied inside the union CTE (single choke point); T10–T12; trends refused for scoped users |
| Agent-repo dependency delays P3 | P1/P2/P4 need no agent change; P3 agent work is capability-gated and degrades gracefully |
| Lifecycle data errors or date changes (e.g. FortiOS 7.4/7.6 extended in 2026) | "As of" date on every value; overrides with reason and expiry; feed refresh between releases |
| Widening software visibility to `inventory.view` holders | Explicit owner decision D3; tag scope still applies; release note |
| Scope creep into CVE matching | P6 is explicitly the Compliance set's phases; nothing CVE-related before P5 completes |

## 6. KPIs to track after P1

- Identity coverage % per field (vendor, model, OS version, serial)
- Fresh-inventory %
- Unmatched-software share
- Median time for an operator to answer an inventory question (UAT)
- Weekly active users of `/inventory/*`
- Exports per week
- From P5: policy pass rate and exception count by age

## 7. Decisions for the owner

| # | Decision | Recommended default |
|---|---|---|
| D1 | Group name and placement | **"Inventory & Compliance"** group after Servers; short label "Assets" |
| D2 | Relabel Servers → "Inventory" to "All Servers" | **Yes**: one meaning for "Inventory" |
| D3 | Does `inventory.view` alone reveal per-server software lists (today `apm.view`)? | **Yes, within tag scope**: the inventory is the point of the module |
| D4 | Do built-in viewers get `inventory.export`? | **No**: export is classified *sensitive*; operators and admins only |
| D5 | Linux coverage path | **Agentless SSH first** (P3.4, reuses `ncm_credentials`); a Linux agent GA stays in the agent roadmap |
| D6 | Lifecycle data source | **Bundled endoflife.date snapshot per release + signed refresh**; no direct appliance egress |
| D7 | Accept the Compliance-set amendments (`00-INDEX.md`, reconciliation table) | **Yes** |
| D8 | Ship P0 dark in the next patch release, ahead of the UI | **Yes**: history accrues from day one |
