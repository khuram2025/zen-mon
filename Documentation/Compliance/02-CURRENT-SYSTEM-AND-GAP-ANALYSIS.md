# 02 — Current System & Gap Analysis

*Status: Verified against the working tree 2026-08-18 (branch `feat/udt-module`, last release 1.15.2). Every claim carries a file:line citation; live-DB observations are from this appliance's Postgres. Full evidence: `raw/code-*.md`.*

The short version: **ZenPlus already owns ~70% of the substrate this module needs.** Asset identity collection, software inventory, a working per-(asset, rule) compliance engine, an authenticated signed sync channel to zentryc.com, RBAC, alerting, and a mature dashboard design system all exist. What is missing is: normalized product identity, patch/KB inventory, the vulnerability/EOL data itself, the matching engine, and the UI.

---

## 1. What exists & is verified

### 1.1 Network-device identity (the firmware-matching raw material)

- `devices` carries `sys_object_id, vendor, model, os_version` (migrate-004-snmp.sql:18–22) and `serial_number` + managed-child columns (migrate-069:31–38, partial unique index on serial).
- The Go poller collects sysDescr/sysObjectID **first** on every SNMP poll (30 s scheduler tick honoring per-device intervals; 60 s default and the live setting on all 37 devices) (`poller/internal/checker/snmp/collector.go:72–86`), classifies via `device_profiles.match_rules` regexes (`profile.go` Match/Extract), and upserts with sticky `COALESCE(NULLIF($n,''), col)` semantics (`poller/internal/store/postgres.go:313–331`).
- ENTITY-MIB chassis rows land in `device_entities` (`class='chassis'`, `model_name`, `serial_number`) — **the authoritative model+serial live here, not on `devices`** (live: `C9300-48UXM/FVH2804L79E`, `PA-VM`, `FGT_VM64`; 403 serials on this box, 0 promoted).
- Exact firewall firmware strings live in `device_template_values` (`fgt_fw_version` = `v7.6.7,build3704,260601 (GA.M)`, `pan_sw_version` = `11.1.10-h25`, `f5_version`, plus AV/IPS/Threat signature versions) — polled continuously, per-row `updated_at`, never joined to `devices`.
- Controller-managed children (FortiAPs, FortiSwitches, Aruba APs) sync vendor/model/os_version/serial via pack `children` mappings every 60 s (`managed_device_service.py`).
- Discovery v2 collects `os` (name!), `os_version`, `serial_number` per host incl. a WinRM probe returning OS caption/build/vendor/model/BIOS serial (`discovery_probes.py:431–482`) — but **serial and hardware identity are dropped at import** (`discovery_v2.py:801+`).

**Live coverage on this appliance (37 devices), counted as non-empty values:** vendor 29/37 (8 empty-string from unmapped sysOIDs), model **4/37** (12 rows non-NULL but 8 of those are empty strings), os_version **17/37** (25 non-NULL), serial 0/37. F5's profile has **no extract rules**; PAN-OS **has** an `extract_os_version` rule that can never fire — PA sysDescr carries no version string — so the fix for PAN is the template-value writeback (`pan_sw_version` → `devices.os_version`), not extract rules.

### 1.2 Server identity & installed-software inventory

- `servers` (migrate-030:21–47): `os_type/os_name/os_version/kernel_or_build/architecture`, `collection_mode`, credential links, `device_id` cross-link. Live sample: `Microsoft Windows Server 2022 Standard Evaluation | 21H2 | 10.0.20348.587 Build 20348.587 | x86_64 | agent` — note the **full Windows build incl. UBR is already captured** in `kernel_or_build`.
- **`server_software_inventory`** (migrate-030:263–271): `(server_id, package_name) PK, version, vendor, install_date` — populated by the Go Windows agent from the registry uninstall list, shipped in the `inventory` dict of `POST /api/v1/agents/results/host`, upserted with a prune-on-snapshot idiom (`host_metric_service.py:462–506`). Live rows: Chrome 151.0.7922.110, FortiClient 7.4.3, PuTTY 0.83, ZeroTier 1.16.1…
- Agent fleet plumbing is complete: enrollment tokens, heartbeats, `capabilities` advertisement + `_agent_supports` gating (`servers.py:900`), policies with `feature_flags` jsonb reaching agents via ETag'd config, command queue (`collect_now`, `upgrade_agent`), update rings, MSI publish flow. **Agent source is in a separate repo (`ZenPlus_Agent`)**; only Windows 1.5.x artifacts exist — Linux agent packaging exists but never shipped.
- The agent design doc already lists "Windows update status" as a planned collector (`server-monitoring-04-windows-agent-msi-design.md:387–398`).

### 1.3 A working compliance engine already ships (the template to extend)

`software_baselines` / `software_baseline_rules` / `server_baseline_results` (migrate-030:308–361) + `baseline_service.py`:

- Scope resolution by os_type/site/tags → rule evaluation (`required|prohibited`, `exact|contains|regex`, `min_version`) → **one outcome row per (server, rule)** with latched `first_failed_at` → deduped raise/resolve alerts.
- `compare_versions()` (`baseline_service.py:54–65`) is a reusable tokenized version comparator (`'1.2.10' > '1.2.9'`) — fine for dotted versions, **wrong for Cisco trains and dpkg/rpm epochs** (the module needs ecosystem-native comparators).
- Re-evaluated automatically whenever a software-inventory upload changes (`host_metric_service.py:631–647`) — **the exact hook point where CVE re-matching triggers too.**
- UI: BaselinesPage (`/server-baselines`), ServerDetail Compliance tab with summary strip + evaluate-now (`ServerDetailPage.tsx:2120–2219`).

This validates the whole shape the module needs: scope → match → per-(asset, finding) upsert → alert lifecycle. The CVE engine is the same machine with feed-supplied rules.

### 1.4 The zentryc.com sync rails (the feed transport, already built)

- Appliance-initiated HTTPS only; Bearer `api_key` + `X-Appliance-ID` headers minted by single-use license-key registration (`updater/agent.py:71–125`).
- Resumable sha256-verified downloader (`updater/downloader.py`), Ed25519 detached-signature manifest verification with a ±(30 d/24 h) freshness window (`updater/crypto.py:126–175`), tar path-traversal guards.
- systemd timer cadence (4 h ± jitter) with dashboard-editable interval; on-demand trigger via narrow sudo; file-based root-updater ↔ API status handoff (`.version`, `.schema-status.json`, `update-history.json`).
- Check-in already pushes `node_count`, `schema_status`, `dashboard_build` — adding `feed_status` gives fleet-wide feed observability with **zero server schema change** (`updater/inventory.py:143–196`).
- The subscription object is cached **verbatim as JSON** on every checkin by the updater (`updater/config.py:168 save_subscription`, called from `updater/agent.py:135/166/205`) → a `features: ["compliance"]` entitlement key flows through untouched **in `subscription.json`**. Caution: the server-side `_sync_from_remote` (`subscription.py:101–158`) copies only a whitelist of fields into the local `subscriptions` DB row — the entitlement must be read from the cached JSON, never from that row.
- Live zentryc.com is **Django/DRF**, Cloudflare-fronted, hand-deployed (no git) on 187.77.177.190; the release/rollout schema (releases, rollout_policies, update_history) is the shape to clone for feed releases.
- Egress reality: at least one production site blocks public NTP; `fetch-geoip.py` is deliberately best-effort. **Assume `zentryc.com:443` is the only egress, and sometimes not even that** — air-gap sideload is required, and signed bundles make it safe.

### 1.5 NCM (sibling module, substrate + cautionary tale)

- NCM = config backup/versioning/diff/change-alerts (`ncm.py`, 756 lines). No policy engine, no firmware tracking — the epic explicitly deferred "Compliance policy engine (CIS/PCI), Firmware EoL/CVE via NVD" to Phase 2 (`enahce1/05-EPIC-E4:15–19`). **This module IS that Phase 2, extended to servers.**
- Reusable: `ncm_credentials` is already the cross-module SSH credential store (discovery + agentless servers reference it); `device_ncm.platform` (netmiko type, autodetect-resolved) is the best OS-family signal for network gear; `device_configs` latest-per-device is the substrate for future config-compliance rules.
- Anti-patterns to not inherit: `POST /ncm/run-scheduled` is **unauthenticated**; NCM defined `ncm.view/manage` permission slugs but never enforces them (uses `get_current_user`/`require_operator_user`); `rule_id NULL` alert inserts never dispatch to notification channels; 756-line router with embedded business logic.

### 1.6 Backend & frontend conventions (fully mapped)

- Backend recipe verified end-to-end: router registration in `main.py`, `require_permission` (`core/security.py:131–147`) + permission catalog (`core/permissions.py`), raw-`text()` SQL house style, asyncio startup loops with transaction-scoped advisory locks, KV settings blob, audit logging, migration framework (lockfile-ordered, probe-gated, replay-safe rules) — see `raw/code-backend-patterns.md` §9 for the concrete file-by-file recipe.
- Frontend: React 18 + React Query 5 + Tailwind token system + Radix + Recharts; section-with-tabs layout pattern (UDT/APM exemplars); table furniture kit (`components/servers/tables.tsx`); three sanctioned KPI-card variants; URL-state conventions; permission-gated nav. See `raw/code-frontend.md` for the full design-language cheat sheet — the Compliance UI spec (doc 05) builds on it verbatim.
- Report engine is section-based (Aug 2026) — compliance report sections plug into the existing registry/renderer.

### 1.7 Prior planning alignment

- The competitive matrix scores ZenPlus "–" on "Compliance/FW-EoL/CVE"; only SolarWinds and ManageEngine are Full (2/15) (`enahce1/02:30`). The roadmap slotted this as **E4 Phase 2** in Phase 4 (~2027) — building now pulls it forward, justified by NCM Phase 1 landing early, the live baseline engine, and the differentiation window.
- Positioning: "modern, affordable, on-prem/appliance NMS with built-in NetFlow security analytics" — asset posture extends the security story from traffic to assets and unlocks MSP/compliance buyers.

---

## 2. Gap analysis — what the module must add

### 2.1 Identity & inventory gaps (Phase 0 prerequisites — the feed is useless without these)

| # | Gap | Fix (pinned in docs 03/07) |
|---|---|---|
| G1 | No normalized product identity anywhere — `vendor` free text (`''` vs NULL vs "Palo Alto Networks"), three independent hardcoded vendor lists | `devices.os_family` column + feed-supplied sysObjectID→(vendor, os_family) dictionary; normalization service |
| G2 | Chassis model/serial stranded in `device_entities`; `devices.model` 4/37 non-empty, serial 0/37 | Promotion job: chassis `model_name`/`serial_number` → `devices` (template value ▸ chassis ▸ profile ▸ discovery precedence) |
| G3 | PAN-OS/F5/others: version in `device_template_values` never reaches `devices.os_version` (PAN's extract rule can never fire — no version in sysDescr; F5 has none) | Identity-keys writeback (generalize the children-mapping idea to the device itself) — the sole PAN fix; extract-rule additions only where sysDescr carries a version (verify per vendor) |
| G4 | sysDescr not persisted; extraction not re-runnable; sticky COALESCE never clears stale values; no per-field freshness | Persist `sys_descr`, add identity freshness timestamp + matcher staleness gate (migrate-080) |
| G5 | No OS *name* on devices — IOS vs IOS-XE vs NX-OS (disjoint CVE sets) only implicit in profile | `os_family` (G1) derived from profile/sysDescr rules |
| G6 | `server_software_inventory` lacks arch, package source, source-package, raw name; version-in-name rows; no history | migrate-080 columns + PK widened to (server_id, pkg_source, package_name, arch); agent sends structured fields (registry ARP already carries them) |
| G7 | **No Windows KB/hotfix inventory** and no UBR guarantee | `server_patch_inventory` + agent `patch_inventory_v1` capability (Get-HotFix + CurrentBuild/UBR); `kernel_or_build` already carries UBR on live data |
| G8 | No Linux package inventory in production (Linux agent never shipped) | Linux agent GA is a Phase-2 dependency; SSH/agentless collection later |
| G9 | 8/37 live devices fully unidentified (unmapped enterprise OIDs) | Feed-updatable sysObjectID dictionary (content, not code) |
| G10 | No identity-change history (patch verification "was 17.6.5 → now 17.9.4a" impossible) | `vuln_finding_events` + findings auto-close on version change covers the workflow need |

### 2.2 Data & engine gaps (the module itself)

- **No vulnerability/EOL data model, feed, matcher, or UI anywhere** — repo-wide grep for `cve|vulnerab|eol|end_of_life` in code: zero hits.
- No ecosystem-native version comparators (Cisco trains, dpkg/rpm epoch, PAN `-hN`, Junos `R…-S…`).
- No asset criticality/exposure concept (tags exist and can seed it).
- Alert metric CHECK constraint must be widened by migration for any `compliance_*` metrics (canonical-list supersede pattern, migrate-076 precedent).
- New permissions don't reach existing DB role rows without a backfill UPDATE (verified gap — NetPath operators need manual role edits today; **ship the backfill in migrate-081 (guarded idempotent UPDATEs)**).
- Report engine has no compliance sections.
- The dead `device.firmware_version` display slot on DeviceDetailPage (`:289–295`) is waiting for a real field.

### 2.3 Platform constraints the design must respect

1. **Migrations:** probeable objects only, replay-safe, never edit shipped files, lockfile registration, no long backfills (startup healer pattern instead), GRANT to `zenplus` in guarded DO blocks.
2. **Background work:** every loop runs in every uvicorn worker → transaction-scoped advisory locks (fresh keys 1515074395/96); matching must never run on the request path (perf budget: list APIs p95 < 400 ms at 10k devices).
3. **Egress:** appliance ↔ zentryc.com:443 only; tolerate weeks offline; air-gap sideload mandatory.
4. **Secrets:** `snmp_credentials` passwords are plaintext in PG (known issue) — compliance endpoints must never echo credential material.
5. **Alert dispatch:** findings that must notify need rule-driven alerts (alert_rules metrics), not `rule_id NULL` inserts.
6. **Release discipline:** migration + code ship in one release; `main` fast-forwards; feed content never ships inside migrations (empty feed tables at install are the designed state).

---

## 3. Reuse map (build on, never duplicate)

| Existing asset | Module reuse |
|---|---|
| `baseline_service.py` evaluate/store/alert loop shape | `compliance_match.py` engine skeleton |
| Software ingest hook (`host_metric_service.py:631–647`) | Trigger per-server re-match on inventory change |
| `updater/downloader.py`; `crypto.py` `verify_signature`/`load_public_key`/`sha256_file`; auth headers | Feed sync transport (new feed key; `verify_manifest` itself is OTA-specific — a thin `verify_feed_manifest` checks `built` with the 7-day window) |
| `updater/inventory.py` checkin payload | `feed_status` fleet telemetry |
| Subscription JSON passthrough | `features: ["compliance"]` entitlement gate |
| `ncm_credentials` + `device_ncm.platform` | OS-family signal; future SSH interrogation |
| `device_template_values` firmware metrics | Firewall firmware source of truth |
| Tags / groups / sites / `compliance_asset_meta` | Scoping + criticality |
| Alert engine + `alert_phrasing.py` | `compliance_*` alert metrics with channel dispatch |
| Report engine (section registry) | Compliance report sections |
| `components/servers/tables.tsx`, KPI tiles, TablePanel, severity badges | Entire Compliance UI |
| UDT `KbLink` pattern + zentryc KB site | `/kb/zenplus/compliance` articles |
