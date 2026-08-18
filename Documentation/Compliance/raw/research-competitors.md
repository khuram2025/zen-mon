# Competitor Research: Vulnerability / Compliance Features in Industry Products

**Prepared:** 2026-08-18 — raw research input for the ZenPlus "Compliance & Vulnerability Management" module plan.
**Scope:** How SolarWinds, ManageEngine, Lansweeper, runZero, Axonius, Tenable, Qualys, Rapid7, Action1/NinjaOne/Atera, and open-source tools (OpenVAS/Greenbone, Nuclei, LibreNMS, Wazuh) implement CVE matching, prioritization, EOL tracking, patch workflow, feed sync, UI/UX, and licensing — and what ZenPlus should copy, adapt, or skip.

---

## 1. SolarWinds NCM "Firmware Vulnerabilities" (closest direct analogue for ZenPlus network devices)

### 1.1 Data sources & sync architecture

- Source of truth is **NIST NVD**. SolarWinds does *not* have every customer hit NVD directly: **each night SolarWinds retrieves the latest NVD data and re-hosts it on solarwinds.com**; customer NCM servers then download from SolarWinds, not NIST. This is the exact "vendor-central relay" pattern ZenPlus should replicate with zentryc.com.
- The two distribution files (verbatim URLs, useful as a format reference):
  - `https://downloads.solarwinds.com/solarwinds/data/cve/cve-all.json.zip` — the full CVE corpus (NVD JSON).
  - `https://downloads.solarwinds.com/solarwinds/data/cve/cpematch.json.zip` — the **CPE match feed** (CPE → version-range applicability data). Matching requires both: CVE records reference CPE match criteria; the match feed expands those criteria into concrete product/version ranges.
- **Offline/air-gapped import:** download both zips on an internet-connected machine, copy to the NCM server, then in **Settings → CVE Data Import Settings → "Manage Data Sources"** replace the default HTTPS URLs with local file paths (`cve-all.json.zip` under Manage Data Sources, `cpematch.json.zip` under **"CPE Match Feed"**). Must be repeated on HA backup servers. Data sources accept **HTTP(S) URLs or local server file paths**, with a validate-before-use step.
- Two separately scheduled jobs, both **disabled by default**:
  1. **CVE data import** — daily toggle + "Run at" time; "Run now" for manual trigger; "Delete All" wipes vulnerability data.
  2. **CVE node matching** — daily toggle + "Run at" time (matching is decoupled from feed download).
- **"Vulnerability alert score threshold"** setting (default **5**, i.e. CVSS ≥ 5) controls which matched vulnerabilities raise alerts.

### 1.2 Matching approach

- NCM correlates downloaded CVE/CPE data against the **OS version string already polled from each managed node** (e.g., IOS version from SNMP `sysDescr` / config). No agent, no active scan — pure inventory-vs-database correlation.
- Supported platforms for matching: **Cisco IOS, IOS XE, IOS XR, Cisco ASA, Cisco Nexus (NX-OS), and Juniper**. That's it — the matching is hand-tuned per NOS family, not generic CPE matching across all vendors.
- Known weakness (from SolarWinds' own THWACK forum): false positives — CPE ranges from NVD often say "IOS 15.x" without feature-set/platform qualifiers, so NCM flags nodes that are not actually affected. Their answer is the state workflow (mark **Not applicable**), not better matching. ZenPlus should expect the same and design the triage workflow up front.

### 1.3 UI/UX

- **Entry point:** the Config Summary dashboard. Classic view has a **"Firmware Vulnerabilities" widget** listing CVEs affecting managed nodes; the modern view shows **node counts by severity level per vulnerability**.
- **Vulnerability Summary page** (click a CVE's Entry ID): summary + current state, **link out to the NVD entry**, and a list of potentially affected nodes each with its remediation state.
- **Six workflow states per (CVE, node) pair** — this is the load-bearing UX idea:
  | State | Meaning |
  |---|---|
  | Potential vulnerability | default on match; unverified |
  | Confirmed vulnerability | verified, no remediation planned yet |
  | Not applicable | doesn't affect this node (false-positive triage) |
  | Remediation planned | action scheduled |
  | Remediated | fixed |
  | Waiver | formally exempted |
- **Change State workflow:** checkbox-select rows → "Change State" → pick state from dropdown → optional **comment** → Change. Clicking the State column value opens the **"Change State Details" dialog** showing full state history with timestamps and comments (audit trail).
- **Reports:** three "Vulnerabilities for each Node" reports — grouped by vulnerability (CVE → nodes), by node (node → CVEs), and by remediation stage. Report columns: **Caption/Entry ID (CVE), IOS Version and Image, URL (NIST link), CVSS Score, Severity (None…Critical), State, Last State Change date**.
- Remediation hook: when a vulnerability is flagged, NCM points the operator into its existing **firmware upgrade workflow** (NCM can push IOS images) — detection and remediation live in the same product.

### 1.4 Licensing/packaging

- Firmware Vulnerabilities is included with an **NCM license** (or SolarWinds Observability Self-Hosted Advanced) — not a separate SKU. NCM itself is tiered by managed-node count: **DL50 / DL100 / DL200 / DL500 / DL1000 / DL3000 / DLX (unlimited)**.

---

## 2. ManageEngine — three relevant products

### 2.1 Network Configuration Manager (NCM) firmware vulnerability module

**Data sources & sync:**
- NVD/NIST data relayed through **ManageEngine's central servers** — the customer appliance must be able to reach exactly two domains: **`https://ncm.nimbuspop.com`** and **`https://www.manageengine.com`**. (Same relay pattern as SolarWinds; ZenPlus equivalent = zentryc.com.)
- Daily scheduled sync; user-settable time; **default 2 a.m. nightly** if unset; **"Update Now"** button for on-demand sync.
- **Air-gapped path:** download a version-matched vulnerability dump file, verify a provided **SHA checksum entered into the UI**, upload through the UI; the previous dump is deleted and replaced atomically. Docs warn that modifying the file corrupts the DB — i.e., they ship a signed/checksummed opaque bundle, not raw NVD JSON.
- Also ingests **vendor-specific feeds (Cisco, Juniper)** alongside NVD. "Firmware vulnerability definitions are imported without interacting with devices" — matching is inventory-side only.

**Matching:** running firmware version (from device inventory/config) correlated against the synced DB. Multi-vendor claims: Cisco, Fortinet, Aruba, CheckPoint, Juniper routers/switches/firewalls.

**Workflow states:** **Exposed → Confirmed → Remediated**, plus **Ignore** for deprioritization. "Exposed Devices" is their headline widget metric.

**Patch recommendation (differentiator):** the module "identifies target firmware versions and shows **how many vulnerabilities each upgrade resolves**" — i.e., it aggregates per-CVE data into an upgrade recommendation ("upgrade to 17.9.4a → clears 23 CVEs"). This is the single most valuable UX idea for ZenPlus device-side remediation.

**Reports:** CVE ID Report (severity scores + timelines), Exposed Devices Report, Firmware Version Vulnerabilities Report (risk mapped by version), **Vulnerability Fix Trend Report** (remediation progress over time), Device Vulnerability History Report (audit).

**Licensing:** included in NCM Professional/Enterprise (Free edition = 2 devices). Professional ≈ $238–595/yr, Enterprise ≈ $3,358–8,395/yr depending on device count; Enterprise adds central-probe architecture for multi-site.

### 2.2 Vulnerability Manager Plus (endpoint/server side)

- Agent-based: lightweight agents on Windows/Mac/Linux endpoints do **continuous local scanning** against a DB of **160k+ known vulnerabilities**.
- Findings carry: CVE ID, CVSS severity, affected systems, **patch availability**, and **vulnerability age**.
- Prioritization: blends CVSS with "AI/ML risk scores, vulnerability age, active attack trends" — their answer to VPR/TruRisk.
- **Integrated patching is the pitch:** detect → prioritize → deploy patch from the same console; patches for OS + **850–1,100+ third-party apps**.
- Also bundles adjacent compliance features: **security configuration management (CIS-style misconfig checks), web server hardening, high-risk/EOL software audit, zero-day mitigation scripts** — i.e., "vulnerability management" is packaged as vuln + config-compliance + patch in one module set.
- **Editions (verbatim from edition-comparison page):** Free $0; Professional **$695 on-prem / $895 cloud per 100 workstations**; Enterprise **$1,195 on-prem / $1,545 cloud per 100 workstations**. Notably, the core security features (scanning, assessment, zero-day detection/mitigation, automated patch deployment, config mgmt, web-server hardening, high-risk software audit) are in **all** editions including Free — the paid tiers add multi-technician, RBAC, 2FA, scale.

### 2.3 Patch Manager Plus (patch workflow reference)

- **Test-and-approve pipeline:** create **test groups** per OS; patches deploy to the test group first; after success they sit "tested, waiting for approval"; **automated test-and-approve is Enterprise-only** (Professional = manual approve or auto-approve-on-release).
- **Decline patches:** per-patch or per-application, scoped to all computers or specific groups; declined patches are excluded from automated deployment (the "never patch Java on the POS terminals" control).
- **Deployment policies:** prebuilt or custom (when to install, reboot behavior, user deferral windows).

---

## 3. Asset-centric platforms: Lansweeper, runZero, Axonius

### 3.1 Lansweeper Risk Insights

- **Matching approach:** assigns **CPE identifiers to inventoried software/hardware/OS/firmware**, then correlates CPEs against vulnerability intel. This is the canonical "CPE correlation over an existing asset inventory" design — exactly ZenPlus's situation (inventory already exists, add matching).
- **Data sources:** **VulnCheck** (commercial aggregator that itself ingests NVD/NIST, CISA, MSRC, vendor feeds), **MSRC**, **CISA**. Lansweeper pays VulnCheck so customers get enriched data without NVD's lag — a build-vs-buy signal: the feed pipeline is hard enough that even Lansweeper outsourced it.
- **Vulnerability Details page sections (exact UI structure):**
  - **General** — CVE ID, description, publication dates, CVSS base score, severity bucket (low/medium/high/critical).
  - **Patch Information** — patch availability status + download links.
  - **CVSS** — full vector breakdown (attack vector, complexity, privileges required, C/I/A impact).
  - **Exploitability** — **EPSS score + percentile, exploit maturity (Weaponized / Proof of Concept), active-exploitation flag, threat-actor and ransomware involvement** — sourced from CISA, MSRC, NVD, VulnCheck.
- Dashboard surfaces: which devices a CVE affects, severity, exploitability, patch data, **and who owns the asset** (owner attribution for remediation routing).
- **Lifecycle Insights** (sibling feature): tracks vendor support status per asset — warns before hardware/software hits end of support. EOL and vulnerability are separate but adjacent features in the same "risk" tab.
- **Licensing:** Risk Insights ships in the **Pro plan** (~$439/month for 2,000 assets, annual billing; sold in 1,000-asset increments). Pricing is per-asset — the industry norm for this category.

### 3.2 runZero

- Agentless, credential-less **network fingerprinting** — identifies OS/version from unauthenticated scan traffic, then populates an **"OS EOL" column directly in the asset inventory** (e.g., derives Proxmox VE version from a scan and marks EOL status).
- Query language is the UX: `os_eol_expired:t`, and combinable queries like "CVSSv2 ≥ 6.5 on EOL assets". Saved queries drive dashboards and alerts.
- Positioning: EOL/outdated-OS detection as a *coverage gap finder* for VM programs (finds the unmanaged/OT/IoT stuff Tenable never scans). runZero is not a CVE scanner; it flags **risk categories** (EOL, unsupported, exposed services).
- Licensing: **Community Edition free < 100 assets**; paid per-asset.
- Lesson for ZenPlus: **EOL-expired should be a first-class filterable asset attribute**, not only a report.

### 3.3 Axonius

- Aggregates other tools via **adapters** (Tenable, Qualys, CrowdStrike, …) — "Vulnerability Management Module" extracts CVEs already found by connected scanners and unifies them.
- **Vulnerability Enrichment:** central CVE catalog enriched from **MSRC, VulnCheck, Intel 471, Mandiant**; every CVE instance links asset ↔ CVE ↔ enrichment.
- Distinct pages: **Vulnerabilities page** (unique CVEs) vs **Vulnerability Instances page** (CVE × asset pairs) — a schema-level distinction worth copying: `vulnerabilities` (catalog) vs `vulnerability_instances` (asset findings).
- **Custom risk score builder:** users compose risk formulas from conditions (account risk, business impact, exploitability) per asset type.
- Query Wizard (+AI assist) → saved queries → dashboards/reports; asset enrichment fields (business unit, lifecycle state, criticality) used as filter dimensions.
- Licensing: enterprise per-asset subscription (opaque quotes).

---

## 4. Prioritization models: Tenable, Qualys, Rapid7

The industry has converged on: **CVSS is a descriptor, not a priority**. Every serious product layers threat intelligence + asset context on top. ZenPlus needs *a* risk score; it does not need ML — a transparent formula in the Qualys/Rapid7 style is achievable and defensible.

### 4.1 Tenable VPR (Vulnerability Priority Rating)

- Scale **0–10** (same shape as CVSS so operators can read it). Computed by ML over **150+ features**; key drivers: **vulnerability age, CVSSv3 impact score, exploit code maturity, threat recency**; threat inputs from exploit repos, advisories, malware sightings, social/dark-web chatter.
- Design principle: keeps the CVSS framework but **replaces the exploitability/exploit-maturity components with a live threat score**.
- Efficiency claim (useful for ZenPlus marketing copy): ~400 VPR-Critical ≈ the protective value of ~9,000 CVSSv3-Critical.
- **Vulnerability states (exact, table stakes for any tracker):** **New** (first seen) → **Active** (seen again) → **Fixed** (no longer found) → **Resurfaced** (found again after Fixed). API names: open / reopened / fixed.
- **Risk modification UX:** **Accept risk** (suppress) and **Recast severity** (override severity with reason) — dashboards then show CVSS-vs-effective-severity divergence.
- Dashboard conventions: severity-bucketed counts, SLA/mitigation-summary dashboards, per-CVE drill-down with affected-asset list, trend ("cyber exposure") charts.
- Licensing: per-asset subscription (Tenable VM); Nessus Professional per-scanner (~$4k/yr class).

### 4.2 Qualys VMDR — TruRisk

- Three-layer scoring, all names load-bearing:
  - **QDS (Qualys Detection Score)** per vulnerability, **1–100**: CVSS base adjusted by **Real-Time Threat Indicators** (actively exploited, dark-web mentions, **CISA KEV membership**, malware/ransomware association).
  - **ACS (Asset Criticality Score)** per asset, **1–5**: business importance (production DB, internet-facing…).
  - **ARS (Asset Risk Score / TruRisk)** per asset, **0–1000**. Verbatim formula:
    ```
    TruRisk = MIN( ACS * ( wc*Avg(QDSc)*Count(QDSc)^(1/100)
                         + wh*Avg(QDSh)*Count(QDSh)^(1/100)
                         + wm*Avg(QDSm)*Count(QDSm)^(1/100)
                         + wl*Avg(QDSl)*Count(QDSl)^(1/100) ), 1000 )
    ```
    where c/h/m/l are severity buckets, w = per-severity weights, and the `Count^(1/100)` term makes *many* findings matter slightly more than *few* (log-like damping).
- UX: **Inventory → Assets** tab leads with the TruRisk score per asset; reports can expand ARS into its ACS/QDS components (score explainability is a UI requirement, not an afterthought).

### 4.3 Rapid7 InsightVM — Active Risk

- **0–1000 per vulnerability**, blending **CVSS + AttackerKB + Metasploit module existence + ExploitDB + Project Heisenberg honeypot data + CISA KEV + EPSS + asset criticality + internet exposure**. Replaced the legacy "Real Risk" strategy (formal EOL announced).
- Dashboard-card conventions worth copying:
  - **"Vulnerability Findings by Active Risk Score Severity"** — count per severity bucket.
  - **"…by Active Risk Score Severity and Publish Age"** — a **heat map of severity × age** (the single best at-a-glance widget in the category).
- **Remediation Projects:** structured to-do lists — each project = the exact fix (patch/config change/version), the affected assets, a due date, **SLA tracking**, with export to ServiceNow/Jira. Remediation is tracked as *work*, not just as a state flag.

### 4.4 Common prioritization ingredients (all three + Lansweeper + Action1)

1. **CVSS base score** (v3.1; v4.0 arriving in feeds).
2. **EPSS** — free, FIRST.org: daily probability (0–1) of exploitation within 30 days + percentile. API: `https://api.first.org/data/v1/epss?cve=CVE-...` (supports `date=`, `epss-gt=`, `order=!epss`, `envelope=true&pretty=true`); full daily CSV: `https://epss.empiricalsecurity.com/epss_scores-YYYY-MM-DD.csv.gz`. No registration.
3. **CISA KEV** — free JSON catalog of confirmed-exploited CVEs (`known_exploited_vulnerabilities.json`, fields incl. dateAdded, dueDate); universally treated as "fix now regardless of score".
4. **Asset criticality** (user-assigned 1–5 or tags) and **exposure** (internet-facing).
5. **Vulnerability age / patch availability.**

ZenPlus can implement a fully transparent score from ingredients 1–5 with zero ML and zero paid feeds.

---

## 5. Patch-management UX: Action1, NinjaOne, Atera

### 5.1 Action1

- **Update rings** (their flagship concept): endpoint groups arranged inner→outer; a patch advances to the next (larger) ring **only after meeting success-rate and deployment-count metrics** in the current ring; failing patches are auto-stopped. Rings double as maintenance windows (regular + emergency).
- **Severity-based SLA:** each vulnerability severity carries a **recommended remediation timeframe** (policy-adjustable); the **"Vulnerability Remediation Compliance" widget** counts **"overdue"** and **"due soon"** items; the **dashboard literally turns green** when the org is fully compliant. Binary, legible compliance signaling — excellent pattern.
- Licensing: **free for the first 200 endpoints with no feature limits** (aggressive land-grab), then ~$4/endpoint/month.

### 5.2 NinjaOne

- **Patching Dashboard** + **Vulnerabilities Dashboard** — the latter maps **CVEs → Windows KB numbers → devices**, so an operator pivots from "CVE-2026-XXXX" to "these 14 machines miss KB503xxxx".
- Reports (exact names): **Patch compliance, Patch enablement, Failed patches, Devices with failed patches, Pending patches, Patch status**.
- Per-device patch statuses: pending / approved / denied / failed / installed. Approval at **policy and device level**; maintenance ("patching") windows; Wake-on-LAN so machines are on during windows; patch caching; Windows/macOS/Linux + ~6,000 third-party apps; "Patch Intelligence AI" flags known-bad patches.
- Continuous evaluation: endpoint patch state vs policy → non-compliant systems surface automatically (no manual audit run).

### 5.3 Atera

- Policy-based patching (schedules, per-device overrides, business-hours avoidance); strong scheduled reporting (patch compliance + historical trends); weaker on risk-based prioritization. Per-technician (not per-endpoint) pricing is its differentiator.

**Distilled patch-UX table stakes:** per-host patch status; compliance % against policy; approve/decline; test-then-promote (rings or test groups); maintenance windows; failed-patch visibility; compliance trend reports.

---

## 6. Open source

### 6.1 OpenVAS / Greenbone (GVM)

- Architecture: **GSA** (React web UI, `gsad`, port 9392) → **gvmd** (manager daemon, port 9390, GMP protocol, results in **PostgreSQL**) → **openvas scanner** executing **NVTs** (50k+ vulnerability test plugins) against targets.
- Feed types via `greenbone-feed-sync`: **NVT feed** (scanner plugins), **SCAP feed** (CVE/CPE data), **CERT feed** (DFN-CERT/CERT-Bund advisories; depends on SCAP being synced first), **GVMD_DATA** (scan configs, port lists, report formats).
- Severity: CVSS v3.1 buckets — Low 0.1–3.9, Medium 4.0–6.9, High 7.0–10.0; GSA dashboards filter by host/vulnerability/severity; reports are CVSS-ranked.
- **Licensing:** components GPL. **Greenbone Community Feed** (free, daily, now *excludes* enterprise-product NVTs) vs **Greenbone Enterprise Feed** (paid, per-asset pricing as of 2026). The monetization lever is **feed content quality**, not the software — directly relevant to zentryc.com packaging.
- Relevance: OpenVAS is an *active scanner* (sends probes). ZenPlus is inventory-driven; active scanning is optional/phase-later. But GVM's feed-separation (scanner logic vs SCAP data vs advisory data) is a good decomposition.

### 6.2 Nuclei (ProjectDiscovery)

- **YAML template DSL** (MIT-licensed engine + community template repo, 8,000+ templates, 2,200+ web CVE templates): each template = id, severity, tags, requests to send, matchers/extractors; protocols include HTTP, TCP, DNS, and a JS module protocol for network checks.
- Community publishes a template within days of a hot CVE — the fastest "detection content supply chain" in the industry.
- Relevance to ZenPlus: a possible *verification* layer ("confirm this flagged device is actually vulnerable") and a model for shipping detection content as data (signed YAML bundles from zentryc.com) rather than code.

### 6.3 LibreNMS (the peer that has nothing)

- **No built-in vulnerability or EOL features.** The community feature request ("Hardware and software inventory and end of support / contract", community.librenms.org t/10581) has sat unimplemented — LibreNMS knows hardware type + software version but does nothing with it.
- Conclusion: in the open-source NMS space ZenPlus competes with, **nobody has shipped this** — the module is genuine differentiation, and the SolarWinds/ManageEngine implementations prove the demand at the commercial tier.

### 6.4 Wazuh — the best architectural reference for the server/agent side

**Vulnerability detection module:**
- **Syscollector** on the agent inventories OS, installed packages, **Windows hotfixes**, hardware, ports, processes on an interval; deltas sync to the server (local SQLite on agent, states DB on server).
- Server-side **Vulnerability Detection module** correlates inventory against the **Wazuh CTI** feed. CTI aggregates **Canonical (Ubuntu OVAL), Debian, Red Hat (OVAL/VEX), Arch, Amazon Linux ALAS, Microsoft (MSRC), CISA, NVD** — i.e., **distro security trackers first, NVD as the generic fallback**. This matters: distro feeds know that Ubuntu backported a fix into `openssl 3.0.2-0ubuntu1.12` while NVD only knows "fixed in 3.0.7"; matching Linux packages against raw NVD produces mass false positives.
- The Vulnerability Detector Provider normalizes everything to **CVE JSON v5 format** (content migration → sanitization → merging vendor content with internal mappings). Feed retrieval via **CTI API or an offline local repository** (air-gap support), diffed against previously stored content so only updates are processed.
- **Match rule:** a package is vulnerable when its version falls within the CVE's affected range (version-range comparison per package ecosystem). Windows OS CVEs are cleared by **hotfix presence** (KB → CVE resolution via Microsoft data).
- **States: Active / Solved** per (agent, package, CVE); stored in indexer index **`wazuh-states-vulnerabilities-*`** (e.g. `wazuh-states-vulnerabilities-server`); UI splits **Inventory tab** (current state) from **Events tab** (alerts on transitions). Alert-noise discipline: no alerts on first sync, on cluster re-sync, or on feed-content updates — **only on real state transitions** (package installed/removed/upgraded, OS/patch change).
- Config: `<vulnerability-detection>` block in `ossec.conf`; `<wodle name="syscollector">` with `<hotfixes>` for Windows.

**SCA (Security Configuration Assessment) — the "compliance" half:**
- Agent runs **YAML policy files** (CIS Benchmark translations, hundreds of rules each; custom policies supported) on an interval (e.g. every 12h). Each check tests a file / process / registry key / command output.
- Results per policy: **passed / failed / not applicable** counts + a **score = % passed** (e.g., Ubuntu 22.04: 191 checks → 56 passed, 87 failed, 48 N/A). Dashboard shows per-agent, per-policy scores with drill-down to individual checks incl. remediation text.
- This is the cheapest credible "Compliance" story: ship benchmark YAML per platform, compute a %-score per device.
- Licensing: GPLv2, feeds free — Wazuh monetizes cloud hosting/support.

---

## 7. Feed & data-source landscape (build notes for the zentryc.com sync)

| Feed | URL / access | Content | Cost |
|---|---|---|---|
| NVD CVE API 2.0 | `https://services.nvd.nist.gov/rest/json/cves/2.0` | All CVEs, CVSS, CPE applicability | Free; rate-limited (2,000 CVEs/page; API key raises limits); legacy JSON feeds retired (API 1.0 dead 2023-12-15) |
| NVD CPE / match | `https://services.nvd.nist.gov/rest/json/cpes/2.0` (+ matchstrings endpoint) | CPE dictionary + match criteria (10,000/page) | Free |
| Community NVD mirror | `github.com/fkie-cad/nvd-json-data-feeds` | Reconstructed legacy-style daily JSON dumps from API 2.0 | Free — good ingestion shortcut for the central server |
| EPSS | `https://api.first.org/data/v1/epss`; daily CSV `epss_scores-YYYY-MM-DD.csv.gz` | Exploitation probability + percentile per CVE, daily | Free, no registration |
| CISA KEV | `known_exploited_vulnerabilities.json` (cisa.gov) | Confirmed-exploited CVEs, dateAdded, dueDate, ransomware flag | Free |
| Cisco PSIRT openVuln API | `https://apix.cisco.com/security/advisories/v2/...` — e.g. `/OSType/iosxe?version=17.2.1`, `/OS_version/OS_data?OSType=ios` | Advisories per exact IOS/IOS-XE/NX-OS/ASA/FTD/FXOS release incl. **first-fixed release**; CSAF format (CVRF being phased out); powers Cisco Software Checker; Critical/High SIR coverage | Free with Cisco API credentials (OAuth2); rate-limited |
| Distro OVAL/security trackers | Ubuntu OVAL, Debian security tracker, RHEL OVAL/VEX, ALAS, MSRC API | Backport-aware fixed-version data per distro package | Free |
| endoflife.date | `https://endoflife.date/docs/api/v1/` (static JSON, Jekyll-built; 464+ products) | Per product: cycles with releaseDate, eol (date or `false`), support, LTS flag, latestVersion, extendedSupport; computed live status | Free, open data (repo on GitHub) |
| VulnCheck / Mandiant / Intel 471 | commercial | Enriched exploit intel (what Lansweeper/Axonius resell) | Paid — skip for v1 |

**Operational cautions:** NVD enrichment backlog (2024–2025) means fresh CVEs may lack CPE data for weeks — supplement with vendor advisories (Cisco PSIRT) for network gear; CVE JSON 5.x with CNA/ADP (CISA "Vulnrichment") containers increasingly carries CVSS/CPE when NVD hasn't analyzed yet. Everyone (SolarWinds, ManageEngine, Wazuh) pre-processes centrally and ships a **compact, checksummed, versioned bundle** to appliances — never make each appliance crawl NVD.

---

## 8. Common UX patterns — what every serious product has (table stakes)

1. **Severity-bucketed summary widgets** (Critical/High/Medium/Low counts) on a dashboard, colored red→green, clickable into filtered lists.
2. **Two pivots, both mandatory:** CVE → affected assets, and asset → its CVEs (Axonius even splits them into separate pages/entities).
3. **CVE detail page:** description, CVSS score + vector breakdown, severity, published/modified dates, **link out to NVD**, patch availability, exploit intel (EPSS/KEV where present).
4. **Per-finding lifecycle state machine** with audit trail: automatic states (New/Active/Fixed/Resurfaced à la Tenable, or Active/Solved à la Wazuh) **plus** manual triage states (Not applicable, Accept risk/Waiver, Remediation planned) with **comment + timestamp history** (SolarWinds' Change State Details dialog).
5. **Nightly central-relay feed sync** with schedule setting, "Update Now" button, last-sync timestamp shown, and an **offline/manual import path** (checksummed file upload) for air-gapped installs.
6. **Severity/score threshold setting** gating alerts (SolarWinds default CVSS ≥ 5).
7. **Filters:** severity, state, vendor/OS, device group/tag, exploit-known (KEV), patch-available, age.
8. **Reports:** by-CVE, by-asset, by-remediation-stage, and a **fix/compliance trend over time**; exportable (PDF/CSV) and schedulable.
9. **Remediation linkage:** the finding points at the fix (target firmware version, KB number, package version) and, where the product can act, into the deploy workflow.
10. **EOL tracked alongside CVEs** as an asset attribute/filter (runZero `os_eol_expired:t`, Lansweeper Lifecycle Insights).
11. **Alert only on state transitions**, never on feed re-imports or re-syncs (Wazuh's rules) — otherwise the module becomes noise and gets disabled.

## 9. Differentiators observed (pick-list for ZenPlus)

- **Upgrade-target recommendation with CVE-clearance count** (ManageEngine NCM: "this version fixes N of your CVEs") — high value, low cost given per-version CVE sets.
- **Risk score above CVSS** blending EPSS + KEV + asset criticality (Tenable/Qualys/Rapid7); Qualys's exact ARS formula is public and reimplementable; keep it **explainable** (show the components).
- **Severity × age heat map** widget (Rapid7).
- **SLA/overdue compliance framing** with a green-when-compliant dashboard (Action1) and severity-based due dates.
- **Update rings / test-group promotion** for patching (Action1/PMP) — relevant only if ZenPlus ever deploys patches; out of scope for flag-and-recommend v1.
- **Remediation Projects** with due dates + ticket export (Rapid7).
- **Config-compliance scoring** via YAML benchmark policies with %-score per device (Wazuh SCA) — the natural "Compliance" pillar next to "Vulnerability".
- **Backport-aware Linux matching via distro feeds** (Wazuh) — correctness differentiator vs naive NVD matching.
- **Vendor-advisory-first matching for Cisco** (PSIRT openVuln first-fixed data) — kills the false-positive problem SolarWinds never solved.
- **Feed as the monetization boundary** (Greenbone community vs enterprise feed; Lansweeper reselling VulnCheck) — zentryc.com can tier: basic NVD/EPSS/KEV/EOL bundle free with license, premium vendor-advisory/faster cadence in higher tiers.

## 10. Implications for ZenPlus (summary of design guidance)

1. **Sync architecture = SolarWinds/ManageEngine relay:** zentryc.com pre-processes NVD + EPSS + KEV + Cisco PSIRT + distro OVAL + endoflife.date nightly into versioned, checksummed bundles (pattern: `cve-all.json.zip` + match data); appliances pull daily (schedule setting, default nightly, "Update Now", last-sync display) via the existing updater channel (`/opt/zenplus/updater` already syncs OTA from zentryc.com — reuse auth/transport). Provide manual upload with SHA verification for air-gapped sites (ManageEngine pattern).
2. **Matching:** devices → per-NOS matchers (Cisco PSIRT version→advisory for IOS/IOS-XE/NX-OS/ASA; NVD CPE fallback for other vendors); servers → Wazuh-style package-inventory vs distro-feed ranges with NVD fallback, Windows via hotfix/KB clearance. Data model: `vulnerabilities` catalog table + `vulnerability_instances` (asset×CVE) table (Axonius split), instance states: auto (new/active/fixed/resurfaced) + manual (confirmed / not_applicable / remediation_planned / waived) + comment history.
3. **Prioritization:** transparent score = CVSS × EPSS/KEV boosts × asset criticality (1–5, reuse device tags) with component display; KEV always surfaces "fix now".
4. **UI:** Vulnerabilities dashboard (severity tiles, KEV tile, severity×age heat map, top risky assets, fix trend), CVE list + CVE detail (affected assets), asset security tab (CVEs + EOL + config score), filters incl. tag/severity/state/KEV/patch-available; EOL as first-class asset column and filter.
5. **Remediation:** recommend target firmware/package version with "clears N CVEs"; track remediation state + trend report; no patch deployment in v1.
6. **Packaging:** module included with license, feed tiered on zentryc.com (Greenbone model); nothing in the open-source NMS space (LibreNMS) has this — clear differentiation.

## 11. Sources

- SolarWinds: documentation.solarwinds.com — `ncm-working-with-firmware-vulnerability-data.htm`, `ncm-managing-firmware-vulnerability-settings.htm`, `ncm-adding-firmware-vulnerability-files.htm`, `ncm-vulnerability-summary.htm`, `ncm_licensing_model.htm`; thwack.solarwinds.com NCM forum (false-positive feature request).
- ManageEngine: manageengine.com — `/network-configuration-manager/firmware-vulnerability.html`, `/network-configuration-manager/Vulnerability-sync.html`, `/network-configuration-manager/network-vulnerabilities.html`, `/vulnerability-management/features.html`, `/vulnerability-management/edition-comparison.html`, `/patch-management/help/test-approve-patches.html`, `/patch-management/help/decline-patches.html`.
- Lansweeper: lansweeper.com (Risk Insights, Pro plan, vulnerability-insights); docs.lansweeper.com `view-the-vulnerabilities-affecting-assets`, `introduction-to-vulnerability-risk-assessment`.
- runZero: runzero.com blog (EOL asset risk, visualize-and-prioritize-risk), help.runzero.com, /pricing, /platform/community-edition.
- Axonius: docs.axonius.com — `vulnerabilities`, `vulnerability-enrichment`, `vulnerability-instances`.
- Tenable: tenable.com/capabilities/vulnerability-priority-rating, VPR-enhancements-FAQ.pdf, docs.tenable.com VulnerabilityStates.htm, what-is-vpr-and-how-is-it-different-from-cvss blog.
- Qualys: docs.qualys.com CSAM `trurisk_score.htm`, VMDR `calculating_asset_risk_score.htm`, qualys.com TruRisk blog series.
- Rapid7: rapid7.com blog "Introducing Active Risk", docs.rapid7.com legacy-risk-strategies-EOL, Active Risk whitepaper/brief.
- Action1: action1.com — /documentation/dashboard/, /blog/patch-management/update-rings/, /patch-management/, /pricing/. NinjaOne: ninjaone.com/docs/patch-management/compliance/. Atera comparisons: superops.com, syncrosecure.com.
- Greenbone: greenbone.github.io/docs (FAQ, workflows), greenbone.net/en/feed-comparison, github.com/greenbone/greenbone-feed-sync.
- Nuclei: docs.projectdiscovery.io/templates/introduction, projectdiscovery.io blog (network templates v9.8.0), github.com/projectdiscovery/nuclei.
- LibreNMS: community.librenms.org t/10581 (unimplemented EOL feature request).
- Wazuh: documentation.wazuh.com — vulnerability-detection/how-it-works.html, sec-config-assessment/how-it-works.html, wazuh.com blog "Introducing Wazuh CTI".
- Feeds: nvd.nist.gov (developers/vulnerabilities, change-timeline), github.com/fkie-cad/nvd-json-data-feeds, first.org/epss (+/api), cisa.gov KEV, developer.cisco.com/docs/psirt, endoflife.date (/docs/api/v1/).
