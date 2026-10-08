# 01 — Research & Gap Analysis

*Status: Research completed 2026-10-07. Codebase claims are verified against `main` @ 482d023 (file:line). Web claims were checked against live sources on 2026-10-07; URLs are inline. The Compliance set's `raw/` reports (2026-08-18) remain valid background, but where they disagree with this file, this file is newer.*

---

## Part A — Market research

### A.1 What leading products show

| Product | Inventory features worth copying | Source |
|---|---|---|
| **Microsoft Defender VM**: software inventory | Columns: vendor, weaknesses, exposed devices, EOS tags. A **version-distribution tab** per product. An installed-devices tab and an **event timeline**. "Software evidence" shows the registry/disk path. A **"Report inaccuracy"** button. Software with no CPE is shown as *not assessable*, not as clean. Refreshes every 3–4 h | learn.microsoft.com/defender-vulnerability-management/tvm-software-inventory |
| **Qualys CSAM / Global AssetView** | Lifecycle stages GA / EOL / EOS / Unknown. Software authorisation states (authorized / unauthorized / needs review), plus "**Required**" software producing *missing required software* findings. Missing security agents per asset class. A left facet pane with counts and group-by | docs.qualys.com/en/csam/latest/inventory/view_software.htm |
| **Lansweeper** | Built-in dashboards: Overview, Assets, **Inventory health**, Security, Software, Lifecycle. Widgets for warranty, BIOS age and "not seen in 30/90 d". Separates **last scan attempt vs last successful scan**. ML publisher normalisation (Google LLC = Google Inc) | docs.lansweeper.com/docs/manage-dashboards · /software-data-normalization |
| **ManageEngine Endpoint Central** | Prohibited software (with auto-uninstall). Alerts on **install/uninstall**, and a recently-installed report. License position (compliant / under / over). PDF/CSV export | manageengine.com/products/desktop-central/software-inventory.html |
| **SolarWinds NCM** | An EoS/EoL workflow: devices grouped into *suggested dates found / assigned / no suggestions*, with **Assign / Ignore / Manual entry**. Cisco and Palo Alto dates imported automatically. Firmware-vulnerability alerts | documentation.solarwinds.com/…/ncm-managing-end-of-support-and-end-of-sales.htm |
| **Auvik** | Lifecycle shown as three milestones (sales, software maintenance, last support). Firmware compared against the vendor's recommended release | support.auvik.com/…/Device-Lifecycle-Powered-by-Aurora |
| **Tanium Asset** | First seen, last used and removal dates, a used/unused flag (90 d), SBOM search | tanium.com/blog/whats-new-in-tanium-asset-tech-talks-112 |
| **Device42 / Snipe-IT** | Prohibited software and license counts. Hardware EOL = purchase date + model EOL months. Audit-due dates | support.device42.com · snipe-it docs |
| **Rapid7 InsightVM** | An "obsolete OS" card. Their own caveat: **EOL software looks clean because people stop filing CVEs against it**, so EOL must be its own signal | discuss.rapid7.com/t/…/26907 |
| **ServiceNow SAM** | Normalisation statuses: *Normalized / Publisher normalized / Partially / Match not found / Manually normalized*, plus local override rules | servicenow.com/docs/…/sam-normalization.html |
| **Intune** | Discovered-apps report (name, platform, version, device count, publisher). Compliance policy with **min/max OS version and valid build ranges** | learn.microsoft.com/mem/intune/apps/app-discovered-apps |

**Converged MVP widget set** (what every serious product shows first):

- **KPI cards:** total assets, % with fresh inventory, EOL / near-EOL OS, unauthorized software, missing required agent.
- **Breakdowns:**
  - OS distribution (family → release → build);
  - **version sprawl** (distinct versions per product);
  - top publishers and categories;
  - network hardware by vendor/model with firmware train.
- **Activity and health:**
  - recently installed or removed software;
  - stale or unseen assets.

### A.2 Normalisation practice

- **Keep two layers and never overwrite raw values.**
  - *Raw evidence:* display name, publisher, version, source (registry hive, dpkg, rpm, SNMP), install date, path.
  - *Normalised:* publisher, product, edition, **cycle** (the major or "market" release), full version, category.
  - Raw evidence is what makes "report inaccuracy" and local rule fixes possible.
- **Treat noise as categories, not deletions.** Windows uninstall entries that are updates or hotfixes (`SystemComponent=1`, `ParentKeyName`, `ReleaseType` Update/Hotfix, or `KB\d{6,7}` in the name) belong in a patch view. Runtimes (VC++ redistributables, .NET, Java) collapse into families but stay visible, because they matter for EOL and CVEs. Linux `-libs`/`-devel` packages are an "OS component" category that is hidden by default.
- **Version comparison is per ecosystem:**
  - dpkg (epoch:upstream-revision, with `~` sorting), RPM EVR, Windows four-part, semver;
  - vendor trains: Cisco IOS `15.2(7)E8`, IOS-XE `17.09.04a` (CPE drops the zero padding), FortiOS `7.4.3 build2573`, PAN-OS `11.1.2-h3`, Junos `21.4R3-S5.4`.
  - The repo has only one tokenised comparator (`baseline_service.py:38-65`), and it is wrong for trains and epochs.
- **CPE 2.3** `vendor:product` is the join key into NVD (e.g. `cpe:2.3:o:cisco:ios_xe:17.9.4`). endoflife.date `/identifiers/cpe` maps about 499 CPE/purl identifiers to its lifecycle products, which is a free bridge from inventory to lifecycle.

### A.3 Lifecycle (EOL/EOS) data

- **endoflife.date API v1**
  - Base URL: `https://endoflife.date/api/v1`. Endpoints: `/products`, `/products/full`, `/products/{p}/releases/{r}`, `/products/{p}/releases/latest`, `/identifiers/{type}`.
  - Envelope: `{schema_version, generated_at, last_modified, result}`. Release fields: `name, codename, releaseDate, isLts, isEoas/eoasFrom, isEol/eolFrom, isEoes/eoesFrom, isMaintained, latest{name,date}`.
  - **`/products/full` is about 3 MB for 481 products**, small enough to ship inside the signed OTA bundle so lifecycle works fully air-gapped. **MIT licensed.**
  - Labels mean different things per product: for Windows "eol" means *Security Support*; for IOS-XE "eoas" means *Maintenance*. Show the product's own labels.
  - **Covered:** windows, windows-server, rhel, ubuntu, debian, sles, alma, rocky, oracle-linux, mssqlserver, postgresql, mysql, esxi, vcenter, cisco-ios-xe, fortios, panos, big-ip, proxmox-ve, exchange, veeam, openssl, java, dotnet.
  - **Not covered:** Junos, NX-OS, classic IOS, Arista EOS, Aruba, SonicOS, Check Point. These need manual entry or vendor data.
- **Vendor EoX**
  - Cisco EoX API v5 looks up by product ID, serial or SW release (fields `EndOfSaleDate … LastDateOfSupport`, `MigrationDetails`). It needs entitled Cisco API credentials, so it is online-only and optional.
  - Fortinet, Palo Alto and Juniper publish lifecycle pages. Fortinet extended FortiOS 7.4/7.6 support in March 2026, so dates change and every value needs an "as of" date.
- **Presentation**
  - Stages: *Supported → Security-only → EOL → Extended (paid) → Unknown*.
  - Buckets "EOL within 90 / 180 / 365 d". A suggested / assigned / ignored workflow with manual override. An exception register with expiry.
  - **"Unknown" stays Unknown and is never counted as supported.**

### A.4 Vulnerability correlation (Phase P6; summarised from the Compliance set and refreshed)

- **NVD changed in 2026.** Since 2026-04-15 NVD fully enriches only KEV, federal and EO-14028-critical CVEs, roughly 15–20% of the total. CPE-only matching now misses most CVEs.
  - Combine NVD with CNA `affected[]` data, vendor PSIRTs and OSV distro data. This confirms the Compliance set's vendor-advisory-first matching.
  - csa research note 2026-04-29 · infosecurity-magazine.com/news/nvd-enrichment-premarch-2026
- **Bulk sources an air-gapped appliance can consume through a signed bundle:**
  - NVD 2.0 yearly feeds with `.meta` sha256;
  - CISA KEV JSON (`cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json`, CC0, 1,734 entries on 2026-10-04);
  - EPSS daily CSV;
  - OSV per-ecosystem zips.
- **OSV distro data accounts for backported fixes.** Matching RHEL/Debian packages against NVD's upstream version ranges is a known false-positive trap.

### A.5 Standards that inventory-based compliance must answer

| Framework | Requirement | What the module must produce |
|---|---|---|
| CIS Controls v8.1 **1.1** | Detailed, accurate, up-to-date enterprise asset inventory (network address, hardware address, machine name, owner, department, approval), reviewed twice a year | Unified asset list with owner/department; review date; coverage % |
| CIS **1.2** | Address unauthorised assets weekly | Unauthorised-asset view (UDT `authorized=false`, discovered-not-monitored) |
| CIS **1.3 / 1.5** | Active discovery daily; passive discovery | Discovery and UDT already exist; show coverage and freshness |
| CIS **2.1** | Software inventory: title, publisher, install date, business purpose, version | Catalog with first-seen and install date; business-purpose field on products |
| CIS **2.2** | Only *currently supported* software is authorised; unsupported software needs a documented exception | Lifecycle (P4) + EOL-without-exception policy (P5) |
| CIS **2.3 / 2.5** | Address unauthorised software monthly; allowlist authorised software | Allow/deny/required policies with exceptions (P5) |
| NIST CSF 2.0 **ID.AM-01/02/08** | Hardware inventory; software/services inventory; lifecycle management | P1 + P4 |
| ISO 27001:2022 **A.5.9 / A.8.8 / A.8.19** | Asset inventory with owners; technical vulnerabilities; software installation control | P2 (owners), P6, P5 |
| PCI DSS 4.0 **12.5.1 / 12.3.4 / 6.3.3** | Inventory of in-scope components; yearly review of HW/SW technologies for EOL with a remediation plan; critical patches within a month | Tag-scoped inventory report, lifecycle report, patch view (P3) + P6 |

Sources: cas.docs.cisecurity.org/en/latest/source/Controls1/ and /Controls2/ · PCI DSS v4.0 · ISO/IEC 27001:2022 Annex A.

**Auditors expect evidence**, not just screens:

- timestamped point-in-time snapshots (hence `inventory_daily_snapshots` from P0);
- an install/remove history (hence change capture from P0);
- an exception register with expiry;
- policy versioning;
- **coverage**, meaning which assets are *not* inventoried.

### A.6 UX patterns adopted

1. **Every KPI and chart segment is a link** to the pre-filtered table behind it. Filter state lives in the URL, so views are shareable.
2. **Faceted search with counts** (CSAM-style left pane) on the Assets and Software lists.
3. **A product page with tabs** (overview, versions, installed on, timeline) and an **asset flyout first, full page second** (Defender-style). The flyout links to the *existing* device/server detail pages; there is no duplicate asset page.
4. **A version-distribution bar** per product, coloured by lifecycle stage once P4 lands, and an **N / N-1 / older** currency split.
5. **Data freshness on every widget.** Last attempt vs last successful inventory. Per-source badges (Agent, SNMP, WinRM, SSH). Feed age for lifecycle data.
6. **"Unknown" and "not assessable" are visible states**, never folded into a healthy count.
7. **Inventory health is a first-class panel**: missing model, OS or serial; stale; unknown vendor. Each one is a worklist link.

### A.7 RBAC patterns observed

- ServiceNow separates `sam_user` from `sam_admin` (normalisation rules, imports).
- Snipe-IT has granular view / edit / audit / import / reports permissions.
- The consensus split is **view / export / manage catalog / manage policy / approve exceptions**, all scoped by site or group, with every catalog/policy/exception change audit-logged.
- Doc 00 pins the ZenPlus equivalent.

---

## Part B — What ZenPlus has today (verified)

### B.1 Server inventory

| Data | Collected by | Stored in | Notes |
|---|---|---|---|
| OS name / version / build / arch | Windows agent `collectInventory` (`ZenPlus_Agent/internal/collectors/collectors.go:811-827`, gopsutil) | `servers.os_name, os_version, kernel_or_build, architecture` (`migrate-030:21-47`) | Example live value: `Microsoft Windows Server 2022 Standard Evaluation \| 21H2 \| 10.0.20348.587`. The full build with UBR is already captured |
| Installed software | Agent registry Uninstall keys: HKLM x64, WOW6432Node, HKCU (`native_windows.go:424-520`) | `server_software_inventory (server_id, package_name) PK, version, vendor, install_date, updated_at` (`migrate-030:263-271`) | Agent **filters out updates/hotfixes** (`:497`) and **caps at 1,000 entries** (`:439`). Ingest **drops** `architecture`, `source` and `install_location` (`host_metric_service.py:614-637`) |
| CPU / RAM / disks | `hardware.go`, `hardware_windows.go` (Win32_DiskDrive) | `servers.cpu_model, cpu_cores, cpu_physical_cores, memory_total_bytes, physical_disks` (`migrate-042`, `migrate-089`) | — |
| NICs, filesystems, services, processes | Agent, every 60 s, digest-suppressed | `server_*_inventory` tables | Full snapshots |
| System manufacturer / model / serial / BIOS / virtualisation | **Not collected by the agent** | — | Only the WinRM discovery probe reads them (`discovery_probes.py:436-449`), and import drops them into `description` (`discovery_v2.py:768-800`) |
| Hotfixes / KBs | **Not collected** | — | — |
| Linux packages | **No shipped Linux agent** (`native_other.go` returns `errWindowsOnly`) | — | The `agentless_*` / `ssh` collection modes have no collector behind them |

- **Cadence:** inventory runs every 6 h (`config.go:177`), and the controller can only toggle it on or off. `collect_now` doesn't force inventory (`agent.go:1006`, still `due()`-gated).
- **History: none.** Each software upload hard-deletes rows older than 2 minutes (`host_metric_service.py:640-658`). There is no install/remove event anywhere.
- **API:**
  - per-server only: `GET /servers/{id}/software` (`servers.py:1006`);
  - `/server-monitoring/overview` counts by `os_type` only (`servers.py:3524`);
  - **there is no fleet-wide software or OS-release aggregate.**
- **UI:** `pages/servers/detail/tabs/SystemTab.tsx` `SoftwareSection` (815-963) does client-side filtering and CSV export, and ignores the `truncated` flag.

### B.2 Network-device inventory

| Data | Source | Stored in |
|---|---|---|
| vendor, model, os_version, sys_object_id | Go poller profile match/extract (`poller/internal/checker/snmp/profile.go:167,206`) → `UpsertSystemInfo` (`poller/internal/store/postgres.go:450`, sticky `COALESCE`) | `devices` (`migrate-004-snmp.sql:7-22`) |
| serial, chassis model, firmware | ENTITY-MIB walk → `UpsertEntities` (`postgres.go:713`, chassis promotion ~756) | `devices.serial_number` (`migrate-069`), `devices.firmware_version` (`migrate-126`), all components in `device_entities` |
| Exact firewall firmware, HA, signature versions | Monitoring templates | `device_template_values`: `fgt_fw_version`, `pan_sw_version`, `f5_version`, `f5_serial`, HA modes, AV/IPS/Threat versions (`migrate-062`) |
| Controller-managed children (FortiAP, FortiSwitch, Aruba AP) | `managed_device_service.py:139,193` | `devices` |
| sysDescr | Collected on every poll | **Not persisted** |

**Gaps:**

- PAN-OS and F5 profiles have **no `extract_os_version`** rule, so `devices.os_version` stays NULL for them. The version is in template values, which are never joined to the device.
- Version strings are raw captures, e.g. `7.2.5,build1517`.
- Vendor names are inconsistent: "Palo Alto Networks" vs "Palo Alto"; "Aruba (HPE)" vs "Aruba".
- `DevicesPage.tsx` renders **no vendor, model, OS or serial column** (headers 940-970; serial appears only in the CSV export at 621).
- `GET /devices` has no vendor/model/OS filter (`device_service.get_devices:83`).

### B.3 Endpoints, grouping, scoping

- **UDT** `udt_endpoints` (`migrate-060:29`): MAC, OUI vendor, hostname, IP, `endpoint_type`, **`authorized`**, first/last seen. This is the raw material for CIS 1.1 (end-user devices) and 1.2 (unauthorised assets). There is no OS fingerprinting.
- **Grouping:**
  - devices have free-text `location`, hierarchical `device_groups`, and `tags` JSONB;
  - servers have `site_id`, `environment`, `owner` and `tags`;
  - `servers.device_id` links a server to its SNMP device row, which **must be used to avoid double-counting**.
- **Scoping is tag-based only** (`users.scope_tags`, `core/scoping.py`: `visible_tags`, `jsonb_tags_visible`, `entity_visible`, `scoped_device_filter`). Out-of-scope detail endpoints return 404.
  - **Gap found:** `/servers/{id}/software`, `/services`, `/filesystems`, `/network`, `/compliance` and `/server-baselines/*` don't check scope. This is filed as a separate fix task.
  - Inventory endpoints must not inherit this gap.

### B.4 Compliance engines already shipped

| Engine | Tables | Behaviour | Lesson |
|---|---|---|---|
| Server software baselines | `software_baselines`, `software_baseline_rules`, `server_baseline_results` (`migrate-030:308-361`) | Rules are required/prohibited, matched exact/contains/regex, with min_version and severity. One result per (server, rule). Alerts deduped as `baseline:{rule_id}`. Re-evaluated on every software ingest | The scope → match → per-(asset, rule) upsert → alert lifecycle shape is proven. It has no exceptions and no history. Vendor is ignored |
| NCM config compliance | `ncm_policies`, `ncm_policy_rules`, `ncm_compliance_results`, **`ncm_compliance_exceptions` (owner, reason, expires_at)**, **`ncm_compliance_snapshots`** (`migrate-131`) | 9 built-in hardening packs; evaluated after each capture and daily | The richer pattern. P5 copies its exceptions and snapshots model |

### B.5 Platform conventions the module must follow

- **RBAC:** `PERMISSION_MODULES` (`permissions.py:16-101`), built-in role lists (164-186) with a version-reconciled seed (`role_service.reconcile_builtin_roles`), `require_permission` (`security.py:234`), router-level `policy()` (`route_policy.py:227-248`), and contract tests `test_route_policy_contract.py` and `test_permissions_model.py`.
- **Frontend:**
  - `useCan()` (`stores/auth.ts:52`);
  - nav entries filtered by `usePermittedGroups` (`Sidebar.tsx:61-74`);
  - path-less access-gate layout pattern (`NetflowAccessGate`, `App.tsx:198`);
  - section layout with tab strip and `KbLink` (`UdtLayout.tsx`).
- **UI kit:**
  - `pages/dashboards/shared.tsx`: `KpiCard`, `SectionHeader`, `ShareBar`, `CATEGORICAL`, `chartTooltipStyle`, `fmtCount`;
  - `components/servers/tables.tsx`: `TablePager`, `sortableTh`, `ExportCsvButton`;
  - `components/ui/QueryState.tsx`: `QueryBoundary`, with forbidden/failed/stale states;
  - Recharts for team dashboards;
  - the only CSV formula-injection guard is in `components/availability/exportCsv.ts`.
- **Reports:** section registry `report_sections.SECTION_REGISTRY` (1540) and presets (1659; an `inventory` preset exists at 1726 but covers network devices only); `report_schedules._REPORT_TYPES` (42).
- **Tests and CI:**
  - `smoke-routes.mjs` keeps a hand-maintained `requiredRoutes` list and **doesn't validate `navigation.ts` targets**.
  - The five required checks are server, dashboard, poller, agent-ubuntu and agent-windows (`.github/workflows/development.yml`).

---

## Part C — Gap list (what this module must add)

| # | Gap | Severity | Phase |
|---|---|---|---|
| G1 | No fleet-wide aggregate of OS releases, software titles/versions or hardware models | Blocking for the request | P1 |
| G2 | Software history is destroyed on every upload; no install/remove/upgrade events; no device firmware-change history | High: unrecoverable over time | **P0** |
| G3 | No normalisation: raw names with embedded versions ("Java 8 Update 361 (64-bit)"), inconsistent vendors, raw firmware strings | High: counts are wrong without it | P0 (built-in) → P2 (editable) |
| G4 | Device identity coverage holes: PAN/F5 os_version NULL; sysDescr not saved; os_family absent | High | P0 |
| G5 | Server hardware identity (manufacturer, model, serial, BIOS, virtualisation) not collected | Medium | P3 (agent repo) |
| G6 | No Linux software inventory; no Windows KB inventory | Medium | P3 |
| G7 | No owner / department / criticality on assets (CIS 1.1 fields) | Medium | P2 |
| G8 | No lifecycle (EOL/EOS) data | High for compliance | P4 |
| G9 | No unified policy engine across servers and network; baselines have no exceptions or history | Medium | P5 |
| G10 | Devices list hides vendor/model/OS; no vendor/OS filters on `GET /devices` | Quick win | P1 |
| G11 | Scope not enforced on server sub-resources | Security | Separate task (filed) |
| G12 | Route smoke doesn't validate nav targets | Quality | P1 (companion) |
| G13 | Agent's silent 1,000-entry software cap; the PK collapses same-name packages | Data quality | P0 (guard) + P3 (agent + key widening) |

## Part D — Feature inventory (research → decisions)

Value scores: **[HV]** = high value for an on-prem or air-gapped appliance; *[later]* = deferred.

| Feature | Value | Phase |
|---|---|---|
| Overview dashboard: KPIs, OS / firmware / software / hardware panels, inventory health, recent changes | [HV] | P1 |
| Unified asset list with facets, flyout, freshness, source badges | [HV] | P1 |
| OS and firmware explorer (family → release → build; servers + network) | [HV] | P1 |
| Software catalog + product page (version distribution, installed on) | [HV] | P1 |
| Hardware explorer (network vendor/model/modules; server CPU/RAM/disk) | [HV] | P1 (server manufacturer/model in P3) |
| CSV export of every dataset, audit-logged, injection-safe | [HV] | P1 |
| Change timeline (install/remove/version/firmware/hardware), trend charts | [HV] | P0 capture → P2 UI |
| Editable normalisation catalog: rules, categories, publishers, noise hiding, "report inaccuracy" | [HV] | P2 |
| Asset attributes: owner, department, criticality, lifecycle state, purchase/warranty | [HV] | P2 |
| Saved views (personal or shared) | — | P2 |
| Scheduled inventory reports (PDF/CSV) via the existing report engine | [HV] | P2 |
| Windows KB inventory, Linux packages (agentless SSH), server hardware identity | [HV] | P3 |
| UDT endpoints as an asset kind; unauthorised and discovered-not-monitored assets | — | P3 |
| Lifecycle: bundled endoflife.date snapshot + overrides + hardware EoS import; Lifecycle page and pills everywhere | [HV] | P4 |
| Policies: allow/deny/required/min-version/N-1/EOL-without-exception/stale/required-agent; exceptions with approval and expiry | [HV] | P5 |
| Framework mapping (CIS 1/2, CSF ID.AM, ISO A.5.9/A.8.19, PCI 12.5.1/12.3.4) + audit pack export | [HV] | P5 |
| CVE / KEV / EPSS correlation, triage, remediation | [HV] | P6 (Compliance set) |
| License position, software metering, CIS Benchmarks (SCAP), CMDB sync, AI copilot | *[later]* | P7 |
