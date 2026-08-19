# Compliance & Vulnerability Management — Design Set Index

*Status: Design proposal · 2026-08-18 · Part of the ZenPlus Compliance module design set.*
*This index is the navigation hub and single source of pinned truth. Where a sibling doc conflicts with this file, this file wins. For implementation status, `07-ROADMAP-AND-TASK-LIST.md` wins.*

---

## Thesis

ZenPlus already knows what every asset **is** (network-device vendor/model/OS version via SNMP, server OS + installed-software inventory via agents) but does nothing with that knowledge. The Compliance module turns inventory into **security posture**: a curated vulnerability/patch/EOL feed is built centrally on zentryc.com from authoritative sources (CVE List 5.x, NVD, CISA KEV, EPSS, vendor PSIRTs, distro trackers, endoflife.date), synced to appliances over the existing OTA rails, and matched against inventory on the appliance — producing per-asset CVE findings with a transparent risk score, patch recommendations ("upgrade to 17.9.5 → clears 23 CVEs"), EOL tracking, a triage workflow, alerts, and reports. Appliances never talk to upstream sources; matching is vendor-advisory-first (not naive CPE), so findings stay accurate. Only SolarWinds and ManageEngine have this at all in our lane — no open-source NMS does — and it directly extends the product's on-prem security story to asset posture.

## Reading order

| Doc | Contents | Read when |
|---|---|---|
| `00-INDEX.md` | This file — pinned names, thesis, conflict rules | Always first |
| `01-MARKET-AND-INDUSTRY-RESEARCH.md` | Ecosystem state (NVD collapse, CSAF rise), competitor teardown, UX table stakes, scoring models, positioning | Product framing |
| `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` | Everything ZenPlus already has (verified file:line), and every gap the module must close | Before designing |
| `03-ARCHITECTURE-AND-DATA-MODEL.md` | End-to-end architecture, appliance data model (full DDL), matching engine, risk scoring, feed protocol, background jobs, alerting | Core design |
| `04-FEATURE-SPECIFICATION.md` | Numbered features CV-F1…CV-F14, each a binding contract (behavior, data, API, acceptance) | Building any feature |
| `05-UI-UX-SPEC.md` | The Compliance section: nav, routes, every page with wireframes, component reuse, query keys, polling | Building the dashboard |
| `06-ZENTRYC-FEED-SERVICE-SPEC.md` | Central-server side: ingestion pipeline, curation, publishing, Django endpoints, ops | Building the feed |
| `07-ROADMAP-AND-TASK-LIST.md` | Phases CV-E0…CV-E4, task tables, estimates, dependencies, test plan T1…Tn, risks & rollout | Planning/execution |
| `raw/` | Investigation reports (7 codebase + 5 industry research) — evidence base with file:line citations | Deep reference |

## Pinned names registry (authoritative)

### Product & navigation

| Item | Pinned value |
|---|---|
| Module name | **Compliance** (sidebar group label: "Compliance", short: "Comply") |
| Nav icon | `ShieldCheck` (lucide) |
| Routes | `/compliance` (Overview) · `/compliance/vulnerabilities` · `/compliance/vulnerabilities/:cveId` · `/compliance/assets` · `/compliance/patches` · `/compliance/eol` · `/compliance/software` · `/compliance/settings` |
| Page folder | `dashboard/src/pages/compliance/` (`ComplianceLayout.tsx`, `api.ts`, `types.ts`, `helpers.tsx` + one file per page) |
| Existing-page touchpoints | Device detail gains a **Security** tab; Server detail **Compliance** tab extended with CVE findings; `DeviceDetailPage` firmware slot lights up |
| KB base | `https://zentryc.com/kb/zenplus/compliance` (i-icon per tab, UDT `KbLink` pattern) |
| React Query key namespace | `['compliance', …]` |

### Permissions (RBAC)

| Permission | Grants |
|---|---|
| `compliance.view` | See all Compliance pages, findings, feed status (operator, viewer, read_only + backfill migration) |
| `compliance.triage` | Change finding states, add comments, accept risk (operator) |
| `compliance.manage` | Feed settings, SLA policy, sync-now, bundle upload, criticality edits (operator) |

All endpoints use `require_permission` from day one (unlike NCM). Catalog entry: `("compliance", "Compliance & Vulnerabilities", …)` in `server/app/core/permissions.py`.

### Backend

| Item | Pinned value |
|---|---|
| Router file | `server/app/api/v1/compliance.py` — `APIRouter(prefix="/compliance", tags=["Compliance"])`, mounted in `main.py` |
| Services | `server/app/services/compliance_feed.py` (sync/load) · `compliance_match.py` (matching engine) · `compliance_score.py` (risk scoring) · `compliance_alerts.py` (alert raise/resolve) |
| Background loops | `compliance_feed_loop` (6 h ± jitter) · `compliance_match_loop` (5 min tick, event-driven work) |
| Advisory lock keys | **1515074395** (feed sync) · **1515074396** (matcher) — first unused keys; note pre-existing collisions on …91/…92 |
| Settings storage | `system_settings` key **`'compliance'`** (JSON blob: schedule, thresholds, SLA policy, feed channel) |
| Alert metrics (new, via constraint-supersede migration) | `compliance_critical_open` · `compliance_kev_open` · `compliance_feed_stale` · `compliance_eol_reached` |

### Postgres tables (appliance) — full DDL in doc 03

| Table | Role |
|---|---|
| `vuln_definitions` | Feed cache: one row per CVE in the curated slice (CVSS 3/4, EPSS, KEV, severity, raw record) |
| `vuln_affects` | Exploded match rules per CVE: `(match_kind, vendor, product, version ranges / exact sets / fixed_in)` |
| `vuln_findings` | Asset × CVE instances: auto state + triage state + confidence tier + evidence + fix |
| `vuln_finding_events` | Append-only state-change audit trail with actor + comment |
| `vuln_remediations` | Computed remediation actions per asset ("upgrade to X clears N CVEs", "install KB…") |
| `eol_definitions` | Feed cache: product lifecycle cycles (EOL/EOAS dates) |
| `eol_findings` | Asset × lifecycle milestone findings (unique per asset+cycle+milestone) |
| `compliance_oid_dictionary` | Feed-updatable sysObjectID-prefix → vendor/os_family/device_type dictionary |
| `compliance_product_aliases` | Feed-updatable software-name alias dictionary (normalized name+publisher → canonical product) |
| `compliance_asset_meta` | Per-asset criticality (1–5), exposure flag, exclusion flag |
| `compliance_daily_snapshots` | One row/day posture rollup (trend charts; Postgres, not ClickHouse) |
| `vuln_feed_state` | Single-row-per-channel sync cursor (applied offset, etag, last sync, error) |
| `server_patch_inventory` | New inventory: installed KBs/hotfixes per server |

### Migrations (next free number is 080; numbered in ship order — inventory hardening ships first, in phase CV-E0)

| File | Ships in | Contents |
|---|---|---|
| `migrate-080-compliance-inventory.sql` | CV-E0 | Inventory hardening: `devices.os_family/sys_descr/identity_updated_at`; `servers.distro_id/distro_release/distro_codename`; `server_software_inventory` + `arch, pkg_source, source_package, raw_name, product_code` with PK widened to `(server_id, pkg_source, package_name, arch)`; `server_patch_inventory` |
| `migrate-081-compliance.sql` | CV-E1 | All module tables above (except `server_patch_inventory`, which is 080's) + indexes + guarded idempotent roles-permission backfill UPDATEs + GRANTs (tables and sequences; guarded, BEGIN/COMMIT, probeable) |
| `migrate-082-compliance-alert-metrics.sql` | CV-E1 | `alert_rules_metric_check` supersede — base is **migrate-076** (the current latest constraint owner; re-verify at implementation), its list verbatim + `tpl\_%` clause + `compliance_*` keys |

### Feed protocol (appliance ↔ zentryc.com) — full spec in docs 03 & 06

| Item | Pinned value |
|---|---|
| Channel name | `network-server` (v1; schema major in path) |
| Manifest endpoint | `GET https://zentryc.com/api/v1/vulnfeed/manifest?channel=network-server` (Bearer api_key + X-Appliance-ID — existing OTA auth) |
| Artifacts | `https://zentryc.com/feeds/vuln/v1/<channel>/…` — immutable `snapshot_*.tar.zst` + `delta_*.jsonl.zst`, sha256 in signed manifest |
| Report endpoint | `POST /api/v1/vulnfeed/report` — applied offset + status, plus (when `share_telemetry` is on, the default) `observed_products` [{vendor, os_family, version, count}] and `unmatched_software` [{name, vendor, count}] — the channel that drives Cisco per-version rule compilation and the alias-dictionary backlog; disclosed in Settings → Data & privacy |
| Air-gap bundle | `.zvb` single-file bundle (manifest + sig + snapshot + aux), pre-authorized URL on the subscription page + dashboard "Upload feed bundle" |
| Signing | Ed25519 detached sig over `manifest.json`; **new key `zentryc-feed.pub`** shipped in the module's release next to `zentryc-release.pub`; per-file sha256 inside the signed manifest; freshness window **7 days** |
| Delta model | Snapshot + monotonic offset log, degrade-to-snapshot (Wazuh CTI shape) |
| Appliance cadence | Manifest poll every **6 h ± 30 min** with ETag conditional GET; stale-feed alert at **> 7 days** |
| Entitlement | Subscription object key `features: ["compliance"]` (flows through existing checkin verbatim) |
| Projected sizes | Curated slice snapshot ~10–40 MB zstd; deltas < 1 MB/day |

### Matching & scoring vocabulary

| Concept | Pinned values |
|---|---|
| Match kinds | `vendor_os` (PSIRT/train rules) · `distro_pkg` · `msrc_build` · `msrc_kb` · `alias_pkg` · `cpe` |
| Confidence tiers | **A** `vendor_exact` · **B** `alias_exact` · **C** `heuristic_high` · **D** `heuristic_low` · **E** `informational` (A+B alert by default; D/E behind a filter) |
| Auto lifecycle state | `open` → `fixed` → `resurfaced` (recomputed each match run; alerts only on transitions) |
| Triage state | `none` → `confirmed` \| `not_applicable` \| `risk_accepted` \| `remediation_planned` (manual, with comment, audited) |
| Finding risk score | **FRS** 0–100: `100 × impact × threat-ladder × exposure × match-quality` (bands: Crit ≥ 70 / High 45 / Med 20); CVSS severity always shown alongside |
| Asset risk score | **ARS** 0–1000: `K(criticality) × (100√nC + 40√nH + 10√nM + 2√nL)`; bands Severe ≥ 850 / High 700 / Med 500 |
| SLA defaults (policy pack, editable) | KEV+ransomware or exposed-KEV 7 d · KEV 14 d · FRS-Critical 15 d · High 30 d · Medium 90 d · Low 180 d · EOL 90 d; packs for PCI / BOD 26-04 / Essential Eight later |
| Exceptions | `risk_accepted` requires comment + supports expiry (`triage_expires_at`); exception log exportable (auditor requirement) |

### Conflict-resolution rules

1. Pinned names here win over any sibling doc.
2. Implementation status: doc 07 wins.
3. Where raw reports disagree with design docs, the design docs win (raw is evidence, not decisions).
4. Migration ground rules from project memory are binding: never edit shipped migrations, every migration probeable, `main` is the release line.
