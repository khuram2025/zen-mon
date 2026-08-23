# Prior-Docs Extraction for the Compliance & Vulnerability Management Module

*Investigator report — raw material only, not a plan. Sources: `/opt/zenplus/Documentation/` planning docs read 2026-08-18. All line numbers refer to the files as of this date.*

Scope of this report:
1. Product positioning and target market (as documented).
2. Every mention of compliance / vulnerability / CVE / patch / firmware / EOL in the prior planning docs, with exact file:line citations and which competitors have what.
3. Prior-planned and already-built assets the new module must align with or reuse.
4. The house documentation style (folder layouts, numbering, section structure, tables, test-plan conventions) that new Compliance docs should follow.

---

## 1. Sources read

**Read fully:**
- `/opt/zenplus/Documentation/enahce1/01-COMPETITIVE-ANALYSIS.md` (307 lines)
- `/opt/zenplus/Documentation/enahce1/02-FEATURE-COMPARISON-MATRIX.md` (71 lines)
- `/opt/zenplus/Documentation/enahce1/03-CURRENT-PRODUCT-INVENTORY.md` (201 lines)
- `/opt/zenplus/Documentation/enahce1/04-ROADMAP.md` (92 lines)
- `/opt/zenplus/Documentation/17-PRODUCT-ENHANCEMENT-ASSESSMENT.md` (209 lines)
- `/opt/zenplus/Documentation/enahce1/05-EPIC-E4-ncm-config-management.md` (154 lines — the epic whose "Phase 2" IS the compliance/firmware-CVE/EoL scope)
- `/opt/zenplus/Documentation/enahce1/00-README.md`, `/opt/zenplus/Documentation/enahce1/06-TEST-PLAN.md`

**Skimmed for context and house style:**
- `/opt/zenplus/Documentation/01-PROJECT-OVERVIEW.md`, `05-UI-DESIGN.md`, `23-SERVER-MONITORING-MODULE.md`, `24-ENTERPRISE-HA-SCALE-ARCHITECTURE.md`, `16-INSTALLER-PUBLIC-GUIDE.md` (zentryc.com channel)
- Polished feature-plan sets: `/opt/zenplus/Documentation/ApplicationMonitier/00-INDEX.md` and `/opt/zenplus/Documentation/features/server-monitoring-00-index.md`
- Grep sweep of the whole `Documentation/` tree for `compliance|vulnerab|CVE|patch|firmware|EOL|end-of-life`.

---

## 2. Product positioning & target market (documented)

Source: `enahce1/01-COMPETITIVE-ANALYSIS.md` (baseline 2026-06-03; 52-point capability taxonomy scored against 15 competitors — ZenPlus full on 14, partial on 23, absent on 15; lines 11–14).

**Strategic thesis / wedge** (lines 30–37, restated at 275–280): ZenPlus should own the wedge between **expensive SaaS observability** (Datadog/Dynatrace/Kentik — "bill-shock, overkill for NetOps"), **heavy legacy on-prem** (SolarWinds — "complex, costly, Windows/SQL-bound"), and **DIY open-source** (Zabbix/LibreNMS/Grafana — "high effort"). Winning identity, verbatim:

> **"a modern, affordable, on-prem/appliance NMS with built-in NetFlow security analytics, transparent device-based pricing, and an agentic-AI copilot."**

**Target market:**
- Mid-market → enterprise and **MSP** buyers; the CRITICAL gap list is explicitly framed as "Where we're blocked from enterprise/MSP deals" (line 22).
- A focused **MSP motion** is recommendation #7 (lines 296–298): multi-tenancy + per-device pricing + PSA/ticketing (ConnectWise/Autotask/ServiceNow) + branded reports, "especially in the **MEA/on-prem markets** the live deployment already targets (**Riyadh/Jeddah sites**)".
- Pricing strategy (recommendation #2, lines 281–283): **transparent device-based, all-features-included pricing** (ManageEngine/Domotz model) as a weapon against PRTG sensor sprawl and metered-SaaS bill shock — operationalized via the built-in Licensing/Subscription + OTA.
- Delivery-model differentiator (line 176–177): "One-line installer, appliance, **built-in OTA updates**, built-in **Licensing/Subscription** — an on-prem/sovereignty story competitors charge a premium for." A Compliance/Vuln module strengthens exactly this on-prem/sovereignty + "unlocks MSP/compliance buyers" story (see E4 §1, quoted in §4 below).
- Vision statement (`01-PROJECT-OVERVIEW.md:3-4`): "production-grade, scalable network monitoring platform capable of monitoring 10,000+ devices."

**Why compliance/vulnerability fits the positioning:** the product already markets a flow-based *security* analytics differentiator ("NDR-lite", 01-COMPETITIVE-ANALYSIS.md:230-233, 290-292). A CVE/EoL/patch module extends the security story from traffic to asset posture, and per the matrix (§3 below) is a near-empty column among the affordable competitors (`#F=2` of 15).

---

## 3. Every compliance / vulnerability / CVE / patch / firmware / EOL mention

### 3.1 Feature-comparison matrix — the compliance row and its neighbors

`enahce1/02-FEATURE-COMPARISON-MATRIX.md`, section **D. Network Config Mgmt** (lines 27–30), verbatim:

```
| **D. Network Config Mgmt** | | | ...
| Config backup/diff | **P** | F | P | P | – | P | F | F | F | – | – | P | – | F | – | F | 6 |
| Config push/automation | **–** | F | – | P | – | – | P | – | F | – | – | – | – | – | – | P | 2 |
| Compliance/FW-EoL/CVE | **–** | F | – | P | – | – | P | P | F | – | P | – | – | – | – | P | 2 |
```

Column key (line 5): SW SolarWinds · DD Datadog · ZBX Zabbix · DT Dynatrace · PRTG · LM LogicMonitor · AUV Auvik · ME ManageEngine OpManager · NAG Nagios/Icinga · CMK Checkmk · LNMS LibreNMS/Observium · TE ThousandEyes · KTK Kentik · GRAF Grafana · DOM Domotz/NinjaOne. `#F` = count of the 15 competitors with the feature *fully*.

**Decoded — "Compliance/FW-EoL/CVE" per competitor:**

| Score | Competitors |
|---|---|
| **Full (F)** — only 2 of 15 | **SolarWinds**, **ManageEngine OpManager** |
| **Partial (P)** | Zabbix, LogicMonitor, Auvik, Checkmk, Domotz/NinjaOne |
| **None (–)** | **ZenPlus**, Datadog, Dynatrace, PRTG, Nagios/Icinga, LibreNMS/Observium, ThousandEyes, Kentik, Grafana |

So the capability is *not* table-stakes (`#F=2`), but the two vendors that have it fully are ZenPlus's **most direct Lane-A competitors**, and both monetize it inside their NCM product. It is a parity-with-leaders / differentiation-vs-peers play, not a deal-blocker.

### 3.2 Competitive analysis — competitor capability call-outs

`enahce1/01-COMPETITIVE-ANALYSIS.md`:

- **SolarWinds** (lines 50–55): strengths include "**NCM** with firmware-CVE/EoL"; the "Learn:" line names "**NCM-firmware-CVE**" explicitly as something to copy. Also note the weakness "SUNBURST/CVE security overhang" (line 53) — SolarWinds' own supply-chain history is a sales angle for an on-prem vendor that takes vuln management seriously.
- **ManageEngine OpManager** (lines 57–64): strengths include "NCM firmware-CVE" (line 61); ManageEngine scored highest overall in the matrix (43/52 full).
- **Missing-feature analysis, 🟠 IMPORTANT tier** (line 206): "**NCM phase 2:** bulk config push/templates + **compliance + firmware EoL/CVE** tracking."
- Roadmap summary table row E4 (line 256): "NCM: backup/versioning/diff + change alerts (→ push/compliance)".
- No other CVE/vuln/patch mentions in this file.

### 3.3 Current-product inventory — the gap acknowledged

`enahce1/03-CURRENT-PRODUCT-INVENTORY.md`, capability self-assessment table:

- Line 158: `| D1 | Config backup / versioning / diff | 🟡 | "Backup Config" action on device; no version/diff UI yet |`
- Line 159: `| D2 | Config push / change automation | ❌ | none |`
- Line 160 (verbatim): `| D3 | Compliance / firmware/EoL/vuln | ❌ | none |`

(The inventory's baseline is 2026-06-03. As of 2026-08-04, D1 has since been built — see §5.1.)

### 3.4 Roadmap — where compliance sits in the epic plan

`enahce1/04-ROADMAP.md`:

- **E4 row** (line 20): "E4 | NCM — backup/versioning/diff + change alerts (**→ push/compliance**) | 🔴 | M–L (phase 1 ~6 wk) | Very High | Credentials, device console, scheduler | biggest NetOps category gap; SW/ME/Auvik parity".
- **Phase 4 — Coverage expansion (9–15 months)** (line 57, verbatim): "**E11** Synthetic + WAN/Internet path + BGP · **E4 phase 2** (config push + **compliance + firmware EoL/CVE**)."
- **Dependency map** (line 70, verbatim): `E4 (NCM) ──▶ E4p2 (push/compliance/firmware-EoL)` — i.e., the compliance module was planned as strictly downstream of NCM phase 1 (which is now built).
- Roadmap conventions to inherit: T-shirt effort sizing "S ≤2 wk · M ~3–6 wk · L ~7–12 wk · XL >12 wk for a 2–3 engineer squad, including poller (Go), API (FastAPI), dashboard (React), and DB/migration work" (lines 8–9); sequence rule "deal-blockers first → finish-the-80%-built → differentiators → coverage expansion" (line 10).

### 3.5 E4 NCM epic — the only prior scoping of the compliance module

`enahce1/05-EPIC-E4-ncm-config-management.md`:

- **§1 rationale** (line 4) frames the buyer: NCM "…turns the inert 'Backup Config' stub into a defensible, sticky feature that raises switching cost and **unlocks MSP/compliance buyers**."
- **§2 Out of scope (Phase 2, outlined only)** — verbatim, lines 15–19:

```
### Out of scope (Phase 2, outlined only)
- Bulk config **push**/templates, golden-config remediation.
- Compliance policy engine (CIS/PCI rule sets).
- Firmware EoL / CVE enrichment via NIST NVD feed.
- Real interactive console / TACACS+ change attribution.
```

  This is the **only prior technical direction ever written for the module**: a compliance policy engine keyed to **CIS/PCI rule sets** (config-audit style) and firmware EoL/CVE enrichment **via the NIST NVD feed**. Note: prior docs assumed direct NVD; the new module's stated design (sync from zentryc.com) supersedes that but should be reconciled explicitly.
- **§12 Phased rollout** (line 154): "…→ then Phase 2 (push/templates, compliance, **NIST CVE/EoL**). Dogfood internally on lab Cisco/Palo/Forti before GA."
- Useful build substrate the epic documents (now largely built, see §5.1): device model fields for matching CVEs — `server/app/models/device.py` (`Device`, `vendor`/`model`/`os_version`/`sys_object_id`, `snmp_credential_id`) (line 25); migration convention `scripts/migrate-00N-*.sql` idempotent SQL + `*-clickhouse.sql` siblings (line 29); crypto reuse `server/app/core/crypto.py` AES-256-GCM mirrored by `poller/internal/checker/snmp/crypto.go` (line 23).
- Vendors seen live / dogfood set (line 8): **Cisco IOS/NX-OS, Palo Alto PAN-OS, FortiGate FortiOS** — "the three seen live". These are the priority vendor set for firmware CVE/EoL matching.

### 3.6 17-PRODUCT-ENHANCEMENT-ASSESSMENT.md (2026-05-06) — compliance as an enterprise attribute

This earlier assessment mentions compliance only as an *enterprise-readiness* attribute, never as a product module:

- Line 42 (Enterprise Security gap): "…tenant isolation, secret rotation, and **compliance-ready logs**."
- Line 66 (Market Leader Benchmark list): "Enterprise RBAC, SSO, audit logs, API keys, and **compliance posture**."
- Line 79 (Competitive Gap Matrix, Reporting row): "Scheduled, branded, SLA, **compliance reports** | Medium".
- Line 137 (Target Architecture, Enterprise Layer): "…HA deployment, backup/restore, upgrade validation, and **compliance reports**."
- No CVE/vulnerability/patch/firmware/EOL mentions anywhere in this file. Its recommended roadmap (Phases 1–4, lines 170–204) contains no compliance module; its enduring relevance is the strategic rule at line 208: make the substrate (telemetry, scheduling, alerting, security, tests) reliable before layering intelligence on top.

### 3.7 23-SERVER-MONITORING-MODULE.md — compliance machinery that ALREADY EXISTS (server side)

This is the biggest prior-art surprise: **a software-compliance engine for servers is already live** (module live since 2026-06-10). The new module should extend, not duplicate, it.

- **Storage split table** (line 43, verbatim): `| Compliance | Postgres | 'software_baselines', 'software_baseline_rules', 'server_baseline_results' | — |` — created by `migrate-030-server-monitoring.sql` (Postgres) + `migrate-030-host-metrics-clickhouse.sql` (ClickHouse) (lines 45–47).
- **Software baselines** (lines 59–71, verbatim highlights): a baseline declares software expectations for a class of servers (scope = `os_type` + site + match-all tags); `required` rules (must be installed, optionally at `min_version`+, with `exact`/`contains`/`regex` matching and "robust mixed-numeric version compare") and `prohibited` rules ("package must NOT be installed (e.g. AnyDesk/TeamViewer on servers)"). Outcomes per (server, rule) — **`compliant / missing / outdated / prohibited`** — stored with `first_failed_at`, surfaced in UI, and "raise/resolve alerts automatically (deduped per rule+server)". Evaluation triggers: software inventory upload, baseline CRUD, tag changes, manual *Evaluate now*.
- **Software inventory already flows**: agent sends "registry uninstall list w/ versions+vendors (38 pkgs)" (line 101); last-known inventory upserted into Postgres `server_*_inventory` tables (line 41). This is the raw asset data a server-side CVE matcher needs.
- **UI**: server detail `/servers/:id` has an existing **Compliance** tab among its 11 tabs (line 78); Baselines page at route **`/server-baselines`** (line 81).
- **Patch management was explicitly recommended as the next inventory step** — P1 recommendation #6 (lines 136–138, verbatim):

> "**Patch level**: installed hotfixes/KBs (Windows `Win32_QuickFixEngineering`, `dpkg/rpm` security updates pending on Linux) + pending-reboot flag → feeds baselines ('KB503xxxx must be present') and a **patch-compliance view**."

- Adjacent P1 security-posture recommendations: #7 "**Listening ports / sockets** (pid, process, port, proto) → security posture + auto-detection of roles" (line 141–142); #10 "**Certificate expiry scan** (machine cert store / configured paths)" (lines 144–145); #5 hardware inventory incl. serials and virtualization flag (lines 134–135).
- **Market alignment row** (line 179): `| Software baseline / compliance rules | partial (templates) [Zabbix] | partial [Checkmk] | ✓ (SCA, paid) [Datadog] | ✗ [PRTG] | **✓ built-in** [ZenPlus] |` — and line 186: "Differentiated today: **baselines-with-alerting** and status_reasons (explainable health) are ahead of same-tier competitors." Line 190 lists "uptime/**patch inventory**" among the "biggest remaining gaps to close".

### 3.8 features/server-monitoring-* (planning set, 2026-05-15)

- `server-monitoring-01-market-and-current-system-assessment.md:283`: "Compliance and audit: who installed, rotated, updated, disabled, changed policy, or ran diagnostics." (agent lifecycle auditing)
- `server-monitoring-03-task-list-and-test-plan.md:217`: "## Epic 10: Security And Compliance" (an epic heading in the server-monitoring task list — security hardening of the agent pipeline, not a vuln module).
- Other grep hits for "patch" in this set are `PATCH` HTTP verbs, not patching.

### 3.9 Mentions that are NOT product-compliance (noise filtered out)

- `13-SHIP-READY-MASTER-PLAN.md:612` — "chicken-and-egg vulnerability" (OTA key bootstrap wording only).
- `12-APPLIANCE-BASE-SYSTEM.md:19` — "Firmware | BIOS or UEFI" (appliance VM spec).
- `24-ENTERPRISE-HA-SCALE-ARCHITECTURE.md` — "security items that gate enterprise" (§6, lines 129–140) are appliance-security findings (unauthenticated endpoints, TLS, JWT defaults, release-key on box), relevant background for a *security-themed* module's credibility but not compliance features.
- ApplicationMonitier files mention "compliance" only as PII-scrubbing/data-governance context.

---

## 4. Existing roadmap epics & assets the module should align with

### 4.1 Direct lineage: the module IS "E4 Phase 2"

The prior plan's dependency chain (`04-ROADMAP.md:70`): `E4 (NCM) ──▶ E4p2 (push/compliance/firmware-EoL)`. E4 Phase 1 (config backup/versioning/diff) **is now built**: `24-ENTERPRISE-HA-SCALE-ARCHITECTURE.md` (version assessed 1.3.0, 2026-08-04) lists NCM among shipped feature breadth (line 12: "SNMP, NetFlow, NCM, APM, discovery, agents, sensors, captures, reports"), cites the live endpoint `POST /api/v1/ncm/run-scheduled` ("SSH fan-out", line 134) and "Secrets encrypted at rest with AES-256-GCM (SNMPv3, Windows, **NCM credentials**)" (line 69). So the precondition for the compliance module is satisfied, and the new module's plan should explicitly present itself as the successor to E4 Phase 2 scope (minus config-push, plus the server-side CVE dimension that E4 never covered).

### 4.2 Other epics to align with (from `04-ROADMAP.md` / `05-EPIC-*`)

| Epic | Why it matters to Compliance/Vuln |
|---|---|
| **E7** Server/host agent fleet (Phase 3) | Software inventory + planned patch-level/pending-reboot collection (23-doc P1 #6) is the server-side asset feed for CVE matching. |
| **E2** SNMP trap → events store (Phase 1) | The shared `events` store pattern (feature flags `EVENTS_PIPELINE_ENABLED` etc.) is the house pattern for new event-producing modules; compliance findings should surface as events/alerts through existing dispatch, as NCM's `config_changed` rule type does (E4 §4, line 48). |
| **E5** RBAC + SSO + multi-tenancy (Phase 2) | Cross-cutting test requirement: every new endpoint must pass tenant-isolation + RBAC tests (`06-TEST-PLAN.md:29-31`). RBAC roles/permission catalog have since shipped (migrate-074, `require_permission` dep — project memory). Vulnerability data is sensitive → viewer-gating matters. |
| **E8** AIOps/GenAI copilot (Phase 3) | Analysis recommends a GenAI copilot grounded in "our own metrics/flows/configs + an MCP server" (01:293-295); vuln/EoL data is a natural grounding source ("what should I patch first?"). |
| **E9** Incident/ITSM (Phase 3) | Compliance findings → tickets (Jira/ServiceNow "Planned" lanes on Channels). |
| OTA/licensing channel | The feed-sync-from-zentryc.com design should ride the existing appliance↔zentryc.com channel: `16-INSTALLER-PUBLIC-GUIDE.md:108` — "**zentryc.com | 443 | OTA channel (registration, checkin, download, report) | Continuously after registration**"; updater checks in every 4 h (`24:40`). Constraint to design for: zentryc.com is a documented SPOF "no mirror, no offline bundle" (`24:96`, SPOF #16) and air-gapped deployments are an acknowledged case (`16:337`). |

### 4.3 Reusable substrate named in the prior docs

- **Asset identity for network devices:** `Device.vendor`, `Device.model`, `Device.os_version`, `Device.sys_object_id` (`server/app/models/device.py`, per E4 §3 line 25). NCM config pulls add exact firmware/OS strings.
- **Asset identity for servers:** Postgres `server_*_inventory` (incl. software inventory) + `servers.os_type` etc. (`23:41`).
- **Compliance result pattern to copy:** `software_baselines` / `software_baseline_rules` / `server_baseline_results` with per-(entity, rule) outcome enum + `first_failed_at` + auto raise/resolve deduped alerts (`23:43,59-71`).
- **Migrations:** idempotent `scripts/migrate-0NN-*.sql` (+ optional `-clickhouse.sql` sibling), run by `updater/steps/run_migration.py` (E4 §5); every migration must be probe-able/classifiable for the OTA schema gate (project memory: migrations-must-be-classifiable, never edit shipped migrations).
- **Secrets:** `core/crypto.py` AES-256-GCM if any credentialed collection is added.
- **Alerting:** new rule types wire into the existing alert engine + channels (E4 task 10 pattern).
- **Feature-flag dark-ship convention:** `NCM_ENABLED`-style settings flag gating ticker + router (E4 §12); ship dark, per-entity opt-in.

### 4.4 Positioning constraints from the enterprise assessment (context for scale/perf sections)

`24-ENTERPRISE-HA-SCALE-ARCHITECTURE.md`: single appliance 4 vCPU/15 GiB; API event loop is already a scarce resource (agent ingest saturates ~240 agents); background loops need advisory-lock leader election (`capture_sweeper_loop` pattern, `network_capture_service.py:166-172`); performance budget in the house test plan: "API p95 < 400 ms for list endpoints at 10k devices" (`06-TEST-PLAN.md:33`). A vuln-matching job must be a leader-elected background loop, batch its writes, and never scan on the request path.

---

## 5. Timeline reality check (docs vs. today, 2026-08-18)

- Competitive analysis baseline 2026-06-03; its Phase 1 (0–3 mo) window has elapsed and Phase 2 items are landing: NCM built (see §4.1), RBAC/permissions built (memory: migrate-074), server monitoring live since 2026-06-10, APM phases underway.
- Compliance/FW-EoL/CVE was slotted in **roadmap Phase 4 (9–15 months, i.e. ~2027-03 → 2027-09)** — building it now pulls it forward, which the plan can justify by (a) NCM Phase 1 done early, (b) the already-live server baseline engine, (c) the `#F=2` differentiation window.
- The inventory doc's D3 "❌ none" is still accurate for CVE/EoL/firmware specifically; the *software-baseline* half of "compliance" is no longer ❌.

---

## 6. House documentation style — what new Compliance docs should look like

ZenPlus has **three documented styles**; the strong precedent for a new module design set is (B), with (A)'s epic skeleton reused for per-epic plans inside it.

### A. `enahce1/` competitive-analysis + epic style

- Folder layout: `00-README.md` (index + method), `01-…` analysis, `02-…` matrix, `03-…` inventory, `04-ROADMAP.md`, `05-EPIC-E{n}-{slug}.md` (one per epic), `06-TEST-PLAN.md`.
- **Every epic doc has exactly 12 numbered sections** (`00-README.md:22-26`, verbatim): "goal & rationale, scope, current state (with code paths), target design, data model & migrations, API changes, poller changes, dashboard changes, **task breakdown table**, acceptance criteria, **test-case table (≈18–20 cases)**, and risks & rollout."
- Section conventions observed in `05-EPIC-E4`: `## 1. Goal & competitive rationale` … `## 12. Risks & rollout`; scope split into "In scope (Phase 1)" / "Out of scope (Phase 2, outlined only)"; current state split "Exists & verified:" vs "**Missing:**" with exact `file.py:line` citations; ASCII box-diagram in Target design; data model as bullet-per-table with inline column lists (`id, device_id FK, schedule_cron TEXT, …`); API table `| Method + Path | Purpose | Key fields |`; task table `| # | Task | Area | Est (d) | Depends on |` with areas `db/api/ui/poller/infra`; acceptance criteria as `- [ ]` checkboxes; test table `| ID | Type | Precondition | Steps | Expected |` with IDs `T1..Tn` spanning unit/integration/e2e/manual/security/perf/regression; risks section always covers **feature flag name, security, perf, back-compat, phased rollout**.
- Roadmap style: effort/impact/dependency table with 🔴 Crit / 🟠 Imp / 🟡 Nice tier emoji, T-shirt efforts, "Depends on" and "Unlocks" columns; 4 named phases with month ranges and italicized taglines; ASCII dependency map; "Top risks & mitigations"; "KPIs to track".

### B. `ApplicationMonitier/` design-set style (the most polished; APM module, 10+1 docs) — recommended template

- Folder of numbered docs with a **`00-INDEX.md` that is "the navigation hub and single source of pinned truth"**: an authoritative **pinned-names registry** (routes, table names, ports, key prefixes, migration-file allocation) that "wins" over any sibling; a one-paragraph **Thesis**; a **reading-order table**; explicit conflict-resolution rules ("for implementation status, 09 wins").
- Standard doc sequence: `00-INDEX` · `01-MARKET-RESEARCH` · `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS` (verified file paths + reuse map) · `03-ARCHITECTURE-AND-DATA-MODEL` (full DDL) · `04-FEATURE-SPECIFICATION` (numbered features F1–Fn, each a "binding contract": priority, behavior, data, API, UX, parity, reuse, acceptance) · `05-INSTRUMENTATION/INGESTION` · `06-UI-UX-AND-DASHBOARDS` (routes, sidebar, `?tab=` sets, ASCII wireframes, query-key registry) · `07-ROADMAP-AND-EPICS` (module-prefixed epic IDs like `AM-E1`) · `08-TASK-LIST-AND-TEST-PLAN` · `09-MODERNITY-ASSESSMENT-AND-BUILD-STATE` (status of record) · `10-…` follow-on specs.
- Header line convention: `*Status: Design proposal · 2026-06-21 · reconciled 2026-08-04 · Part of the ZenPlus … design set.*`
- ClickHouse tables documented as `| Table | Engine | Partition | ORDER BY | TTL |`; Postgres config tables listed by name with the rule "High-volume telemetry never lands in Postgres — only definitions/config/registry/…state."

### C. `features/` flat style (lighter): `<feature>-00-index.md`, `-01-…` etc., date line "Date: YYYY-MM-DD" under the H1, per-doc bullet summaries in the index, and a "Main Product Direction" closing paragraph.

### Cross-cutting house conventions (any style)

- Top-level docs are `NN-TITLE.md` in `Documentation/` (next free number would be 26); module design sets get their own folder (`Documentation/Compliance/` already exists for this effort).
- **Evidence-based writing**: claims carry `path/file.py:line` or `file.go:123` citations; "Exists & verified" vs "Missing" honesty; live-deployment observations cited (`http://10.12.50.81/`).
- Diagrams: ASCII box diagrams in epics/feature plans; mermaid `flowchart` in the newest doc (24). Dark-NOC UI tokens for any UI mocks are in `05-UI-DESIGN.md` (bg `#0F1117`, status colors `--status-up #22C55E` / `--status-down #EF4444` / `--status-degraded #EAB308`, accent indigo `#6366F1`, Inter + JetBrains Mono).
- Test discipline from `enahce1/06-TEST-PLAN.md`: seven levels (unit/integration/contract/E2E/perf/security/UAT); cross-cutting non-functionals every epic must pass (RBAC enforcement, tenant isolation, migrations "forward-only… idempotent on OTA", perf budgets, secret safety, OTA upgrade N-1→N); per-epic Definition of Done; "Feature behind a flag until UAT sign-off."
- Release/migration ground rules (project memory, binding): never edit shipped migrations; every Postgres migration must create a probe-able object for the OTA schema gate; `main` is the release line.

---

## 7. Key verbatim snippets (load-bearing)

**The matrix row** (`02-FEATURE-COMPARISON-MATRIX.md:30`):
```
| Compliance/FW-EoL/CVE | **–** | F | – | P | – | – | P | P | F | – | P | – | – | – | – | P | 2 |
```

**The only prior module scoping** (`05-EPIC-E4-ncm-config-management.md:15-19`):
```
- Bulk config **push**/templates, golden-config remediation.
- Compliance policy engine (CIS/PCI rule sets).
- Firmware EoL / CVE enrichment via NIST NVD feed.
- Real interactive console / TACACS+ change attribution.
```

**Roadmap placement** (`04-ROADMAP.md:57,70`):
```
- **E11** Synthetic + WAN/Internet path + BGP · **E4 phase 2** (config push + compliance + firmware EoL/CVE).
E4 (NCM) ──▶ E4p2 (push/compliance/firmware-EoL)
```

**The live compliance engine** (`23-SERVER-MONITORING-MODULE.md:43,66-71`):
```
| Compliance | Postgres | `software_baselines`, `software_baseline_rules`, `server_baseline_results` | — |
...
Outcomes per (server, rule) — `compliant / missing / outdated / prohibited` — are stored
with `first_failed_at`, surfaced in the UI, and raise/resolve alerts automatically
(deduped per rule+server).
```

**The patch-inventory directive** (`23-SERVER-MONITORING-MODULE.md:136-138`):
```
6. **Patch level**: installed hotfixes/KBs (Windows `Win32_QuickFixEngineering`,
   `dpkg/rpm` security updates pending on Linux) + pending-reboot flag → feeds baselines
   ("KB503xxxx must be present") and a patch-compliance view.
```

**The wedge statement** (`01-COMPETITIVE-ANALYSIS.md:33-34`):
```
"a modern, affordable, on-prem/appliance NMS with built-in NetFlow
security analytics, transparent device-based pricing, and an agentic-AI copilot."
```

---

## 8. Implications checklist for the plan authors (derived, one line each)

1. Present the module as the successor of **E4 Phase 2** (compliance + firmware EoL/CVE) extended to servers; NCM Phase 1 dependency is satisfied.
2. Reuse/extend the **server baseline engine** (`software_baselines` family) rather than inventing a second compliance results model; keep its outcome-enum + `first_failed_at` + auto raise/resolve alert pattern.
3. Network-device matching keys already exist: `vendor/model/os_version/sys_object_id`; server matching keys: software inventory rows + planned hotfix/KB inventory (agent P1 #6 must be scheduled as a prerequisite).
4. Feed sync must ride the existing zentryc.com:443 OTA/licensing channel, with an explicit answer for the documented SPOF/no-mirror/air-gap constraints; prior docs assumed direct NIST NVD — reconcile.
5. Competitors: only SolarWinds + ManageEngine are Full; frame as Lane-A parity + affordable-segment differentiator, feeding the security story (NDR-lite adjacency) and MSP/compliance-buyer unlock.
6. Docs: create an ApplicationMonitier-style numbered set under `Documentation/Compliance/` with a `00-INDEX.md` pinned-names registry, module epic prefix (e.g. `CV-E1`), 12-section epic docs, T1..Tn test tables, feature flags, and the `06-TEST-PLAN` cross-cutting non-functionals.
7. Respect platform constraints: leader-elected background matcher, batched writes, Postgres for findings/config only, ClickHouse only if high-volume history is needed, RBAC/permission gating on all endpoints, idempotent probe-able migrations, dark-ship behind a flag.
