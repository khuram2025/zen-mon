# 01 — Market & Industry Research

*Status: Research synthesis · 2026-08-18. Condensed from `raw/research-competitors.md`, `raw/research-cve-sources.md`, `raw/research-feed-distribution.md`, `raw/research-package-matching.md`, `raw/research-frameworks-scoring.md` (all with source URLs) and `raw/code-prior-docs.md`.*

---

## 1. The strategic case

- The house competitive matrix scores "Compliance/FW-EoL/CVE" as **Full for only 2 of 15 competitors — SolarWinds and ManageEngine** — and both are ZenPlus's most direct Lane-A rivals, monetizing it inside their NCM products. ZenPlus scores "–". Partial: Zabbix, LogicMonitor, Auvik, Checkmk, Domotz/NinjaOne. **No open-source NMS has shipped this** (LibreNMS's request has sat open for years).
- This module is the roadmap's **E4 Phase 2** ("compliance + firmware EoL/CVE via NVD"), planned as strictly downstream of NCM Phase 1 — which shipped. Building now pulls it forward from Phase 4, justified by the live server-baseline engine and the open differentiation window.
- It extends the product's stated wedge — "modern, affordable, on-prem/appliance NMS with built-in NetFlow **security** analytics" — from traffic security to **asset posture**, and unlocks the MSP/compliance-buyer segment the NCM epic named. The 2024–26 regulatory wave (PCI DSS 4.0 enforcement, NIST CSF 2.0, NIS2/CRA, HIPAA NPRM, CISA BOD 26-04) moved buyer expectations from "nice reports" to "default remediation SLAs with named-framework mappings out of the box."
- A bonus angle: the EU **Cyber Resilience Act** (reporting of actively exploited vulns mandatory from Sep 2026) makes the zentryc.com pipeline double as ZenPlus's *own* CRA vulnerability-handling machinery.

## 2. The 2026 vulnerability-data ecosystem (design-forcing facts)

1. **NVD enrichment collapsed and the gap is now policy.** Since 2025-04 all pre-2018 CVEs are "Deferred"; since **2026-04-15 NIST only enriches CVEs that are KEV-listed, federal-relevant, or EO-critical (~15–20% of volume)** — everything else gets no CVSS, no CWE, and critically **no CPE applicability data**. A CPE-matching pipeline alone is a dead end for new CVEs. Applicability must come from CNA `affected[]` blocks in **CVE JSON 5.x** (cvelistV5), **CISA Vulnrichment** (SSVC/CVSS/CWE in the ADP container — but no CPEs since 2024-12), and **vendor PSIRT feeds**.
2. **The CVE Program stabilized** after the 2025 funding scare (contract secured into 2026+; the CVE Foundation is a standby hedge) — but mirror-first consumption (cvelistV5 on GitHub: daily baseline zip ~580 MB + hourly ~0.3 MB deltas) is the resilient pattern.
3. **CSAF 2.0 is the de-facto vendor advisory format** — Cisco, Microsoft, Red Hat, SUSE, HPE Aruba publish it; Fortinet (CVRF only), Juniper, MikroTik, Ubiquiti do not → per-vendor adapters remain necessary.
4. **KEV + EPSS are the free prioritization backbone.** KEV: 1,282 entries (2026-08), 234 ransomware-flagged, CC0, with per-entry due dates. EPSS v4/v5: daily scores for every CVE, free CSV. Every serious product blends CVSS + EPSS + KEV + asset context; buyers now ask "do you have a VPR/TruRisk equivalent?" in RFPs.
5. **CISA BOD 22-01 was revoked; BOD 26-04 replaces it** — a four-variable matrix (publicly exposed × KEV × automatable × technical impact, three answered per-CVE by Vulnrichment) yielding 3-day/14-day/60-day/defer tiers. It is SSVC operationalized as policy and the template federal-adjacent buyers will reference in 2026–27.
6. **Distro advisories are ground truth for Linux** (backports make raw NVD ranges wrong — the classic false-positive factory); **MSRC FixedBuild build-number comparison** is the robust Windows approach; **vendor version-query APIs (Cisco openVuln) beat CPE for network firmware** — NVD CPEs for network gear are erratic and now dying.

**Consequence:** the value is in the **curated, pre-joined feed** — which is exactly what zentryc.com becomes. Post-triage-NVD, a well-curated vendor feed is a genuine differentiator, not a convenience.

## 3. Competitor teardown (what to copy, what to avoid)

### SolarWinds NCM "Firmware Vulnerabilities" — the closest analogue
Nightly NVD re-host on solarwinds.com → customers sync from SolarWinds (the central-relay pattern we replicate with zentryc.com); offline import via file-path data sources; two decoupled scheduled jobs (feed import / node matching), disabled by default; CVSS ≥ 5 alert threshold setting. Matching = polled OS version vs CVE/CPE data, hand-tuned for Cisco IOS/XE/XR/ASA/Nexus + Juniper only. **Its known failure: CPE-driven false positives** (their forums are full of them); their answer is the state workflow, not better matching. **Copy:** the six workflow states per (CVE, node) — Potential / Confirmed / Not applicable / Remediation planned / Remediated / Waiver — with comment + timestamped state history dialog. **Avoid:** NVD-CPE-only matching.

### ManageEngine (NCM + Vulnerability Manager Plus) — the feature bar
NCM firmware module: central relay (two domains), nightly sync + "Update Now", checksummed opaque dump for air-gap, states Exposed→Confirmed→Remediated + Ignore. **The killer feature: upgrade-target recommendation — "this firmware version resolves N of your vulnerabilities."** VMP (endpoint side): agents + 160k-vuln DB, CVE/CVSS/patch-availability/age per finding, integrated patching, plus config-compliance (CIS), EOL-software audit — "vulnerability management" packaged as vuln + config + patch. **Copy:** the upgrade recommendation with clearance count (our `/compliance/patches` page is built around it).

### Wazuh — the best architectural reference for the server side
Agent Syscollector inventories packages + Windows hotfixes → server-side matching against **Wazuh CTI** (distro feeds first, NVD fallback; everything normalized to CVE JSON 5.x; alias/"CPE-helper" dictionaries shipped as content). States Active/Solved per (agent, package, CVE); **alerts only on real state transitions — never on feed re-imports**. Snapshot + offset-log content distribution (~312 MB full corpus, few-thousand-record daily deltas) with degrade-to-snapshot. **Copy:** nearly everything about its pipeline shape; our curated slice is far smaller (10–40 MB).

### Tenable / Qualys / Rapid7 — the prioritization models
VPR (0–10, ML, ~"400 VPR-criticals ≈ 9,000 CVSS-criticals"), TruRisk (QDS 1–100 per detection + ACS 1–5 + ARS 0–1000 with the published damped-sum formula), Active Risk (0–1000, KEV+EPSS+Metasploit+honeypots). Convergent lesson: **CVSS is a descriptor, not a priority**; add threat + asset context; keep it **explainable** (Qualys exposes components; VPR's opacity is the common complaint). Tenable's states (New/Active/Fixed/Resurfaced) and Accept-risk/Recast-severity UX; Rapid7's **severity × age heat map** and Remediation Projects (fix + assets + due date + SLA) are the UX standouts. A transparent formula from CVSS+EPSS+KEV+criticality needs no ML and no paid feeds — that is our lane.

### Patch-management UX (Action1 / NinjaOne / Atera)
Severity-based SLA with "overdue / due soon" compliance widgets and a **dashboard that turns green when compliant** (Action1); CVE→KB→devices pivot (NinjaOne); per-host patch status, approve/decline, maintenance windows, compliance trend reports. v1 of our module is flag-and-recommend (no deployment), but the SLA/compliance framing applies fully.

### Open source & scanners
Greenbone/OpenVAS: feed content is the monetization boundary (community vs enterprise feed) — the model for tiering zentryc feed content. Nuclei: detection content as signed data, community-fast — a future active-verification layer. Grype/Trivy: daily full-replace DB distribution with tiny discovery manifests (`latest.json` + immutable artifacts) — the distribution pattern we adopt. Lansweeper: buys its feed from VulnCheck (build-vs-buy signal: the pipeline is the hard part — and it is exactly the part zentryc.com owns).

## 4. UX table stakes & differentiators

**Table stakes (every serious product):** severity-bucketed clickable dashboard tiles; both pivots (CVE→assets and asset→CVEs); CVE detail with CVSS vector, EPSS/KEV, patch availability, NVD/vendor links; per-finding lifecycle + manual triage states with comment history; nightly central-relay sync with "Update Now", last-sync display, offline import; severity threshold gating alerts; filters (severity/state/KEV/vendor/tag/fix-available/age); by-CVE, by-asset, by-stage and trend reports; remediation linkage (target version/KB); EOL as a first-class asset attribute; **alerts only on state transitions**.

**Differentiators we adopt:** upgrade-target recommendation with CVE-clearance count (ManageEngine); transparent explainable risk score (anti-VPR); severity×age heat map (Rapid7); green-when-compliant SLA framing (Action1); backport-aware Linux matching + vendor-advisory-first Cisco matching (correctness — kills the SolarWinds FP failure mode); confidence tiers with evidence on every finding (Grype/Dependency-Check); BOD 26-04 action buckets as a lookup overlay (nobody mid-market has it).

## 5. Compliance frameworks — what buyers and auditors expect

"Compliance" means two surfaces, usually two tabs of one area sharing inventory + reporting: **(a) vulnerability/patch compliance** (this design set's scope) and **(b) configuration/framework compliance** (CIS/STIG/PCI config checks with per-rule pass/fail, compliance %, remediation text — CV-F14, outlined for phase 2). The universal architecture for (b) is two-layer indirection: checks → control objectives → framework controls, all as data (Qualys mandate reporting spans 90+ frameworks from one control library; CIS publishes benchmark→controls→CSF/ISO/PCI mappings to reuse).

What auditors concretely ask of surface (a) — encoded as product requirements in docs 03–05: an SLA policy visible in-product; **12 months of scan/finding history**; severity categorization everywhere; a **risk-acceptance/exception log with owner, justification, expiry**; remediation records with before/after (rescan) evidence; asset-scope documentation. Key SLA anchors: BOD 26-04 tiers (3/14/60/defer); PCI DSS 4.0.1 critical/high patches ≤ 1 month + quarterly rescan-to-clean cadence; Essential Eight 48 h for exploited internet-facing; HIPAA NPRM (proposed) 15/30-day ladder; industry default ladder Critical 15 d / High 30 d / Medium 90 d / Low 180 d. KPI canon (dashboard + reports): risk trend, MTTR by severity (mean+median; benchmark: median ~43 d industry, target < 7 d critical), SLA attainment ≥ 90%, aging buckets, patch compliance % (eligibility-scoped), KEV exposure (target 0 past-due), EOL counts, fix rate (target 1.2–1.5), scan coverage ≥ 95%, exception count, recurrence rate.

## 6. Source & licensing posture (full matrix in raw reports)

Freely redistributable through zentryc: CVE List 5.x (attribution), NVD (public domain), KEV + Vulnrichment (CC0), EPSS (attribute), OSV/GHSA (CC-BY), MSRC, distro feeds, Aruba CSAF, endoflife.date (MIT). Requires care: **Cisco openVuln** (free API; bulk re-serving is a ToS gray zone → serve derived rules for fleet-observed versions; legal review before GA), Cisco EoX (entitlement-bound, server-side only), Juniper (portal scrape; CNA records preferred), Fortinet CVRF (public, verify terms). Per-vendor machine-readable quality, best→worst: Cisco (version-query API) > MSRC > Red Hat/Ubuntu/Debian > Aruba (CSAF) > Palo Alto (beta API) > Fortinet (CVRF) > Juniper/Ubiquiti (CNA records) > MikroTik (curation).

## 7. Packaging & monetization

- Module included with the product license (SolarWinds/ManageEngine bundle it with NCM — a separate SKU would undercut the "all-features-included transparent pricing" positioning).
- **The feed is the commercial boundary** (Greenbone model): entitlement-gated via the subscription object; a future premium tier can add faster cadence, Cisco EoX hardware EOL, BOD 26-04 pack, config-compliance packs — without touching appliance code.
- Fleet feed-freshness telemetry doubles as a support tool (stale appliances visible centrally) and as usage evidence for renewals.
