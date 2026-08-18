# Research: Compliance Frameworks, Risk Scoring, and Reporting Norms
### Raw research input for the ZenPlus "Compliance & Vulnerability Management" module

- **Date:** 2026-08-18
- **Method:** Web research (vendor docs, CISA/NIST/FIRST/PCI primary sources, practitioner guides current to Aug 2026)
- **Audience:** Engineering-plan authors. This is a data document; numbers and URLs are load-bearing.

---

## 0. Executive framing: what "COMPLIANCE" means to enterprise buyers in this market

Buyers of SolarWinds/ManageEngine-class tooling use the word "compliance" for **two distinct product surfaces**, and expect both to exist (even if one ships later):

1. **Vulnerability / patch compliance** — "Are my assets free of known-exploitable software, and are we patching within our SLAs?" Objects: CVEs, patches, EOL software, remediation SLAs, risk scores. Evidence: scan history, MTTR, SLA attainment, exception log.
2. **Configuration / framework compliance** — "Do my device and server configurations satisfy a named external standard (CIS, STIG, PCI, HIPAA...) and can I hand an auditor a report proving it?" Objects: benchmark rules, per-device pass/fail checks, control mappings, compliance %, remediation scripts.

Every serious competitor exposes both, usually as separate tabs of one "Compliance" area, joined by a shared asset inventory and a shared reporting engine. The 2024–2026 regulatory wave (PCI DSS 4.0 enforcement, NIST CSF 2.0, HIPAA NPRM, NIS2/CRA, CISA BOD 26-04) has moved buyer expectations from "nice-to-have reports" to "default remediation SLAs with named-framework mappings out of the box."

Three **August-2026 facts** that must shape the design (all detailed below):

- **CISA BOD 22-01 is revoked.** BOD 26-04 (effective mid-2026, full timelines by ~Dec 7, 2026) replaces flat KEV deadlines with a 4-variable matrix producing 3-day / 14-day / 60-day / defer tiers. KEV due dates are no longer the whole story for federal-adjacent buyers.
- **NVD no longer enriches most CVEs.** Since April 15, 2026 NIST only fully enriches CVEs that are in KEV, in federal software, or EO-14028-critical (~15–20% of volume); ~29k backlog CVEs marked "Not Scheduled." A curated vendor feed (zentryc.com) is now a genuine differentiator, not a convenience — but it must aggregate CVE.org JSON 5.x + CISA Vulnrichment + EPSS + vendor PSIRTs, not just mirror NVD.
- **EPSS v4 (Mar 2025) and CVSS v4.0 are mainstream.** Mature products blend CVSS + EPSS + KEV + asset context into one 0–1000 or 0–10 score; buyers now ask "do you have a VPR/TruRisk equivalent?" in RFPs.

---

## 1. The two meanings of compliance, and how products implement each

### 1.1 Meaning (a): vulnerability / patch compliance

Definition in practice: percentage of in-scope assets whose known vulnerabilities and missing patches are within policy. The product loop is: **inventory → match against vuln/patch/EOL intelligence → score → assign SLA due date → track remediation → report attainment**.

What the leading products ship:

- **ManageEngine Vulnerability Manager Plus** (closest analog to the ZenPlus module): vulnerability scanning + assessment, automated patch management, CIS-benchmark compliance, security configuration management (config-drift detection and secure-config deployment), zero-day mitigation scripts, **high-risk / EOL software audit**, web-server hardening. Multiple dashboards: endpoint vulnerability, security configuration, patch status. (https://www.manageengine.com/vulnerability-management/features.html)
- **ManageEngine Patch Manager Plus**: "patch compliance" = continuous evaluate-remediate cycle toward 100% patched; per-SLA compliance windows; point-in-time and rolling-period views. (https://www.manageengine.com/patch-management/patch-compliance.html)
- **Tenable**: per-vuln VPR + per-asset ACR; SLA dashboards ("SLAs and Remediation" cyber-exposure study: https://docs.tenable.com/cyber-exposure-studies/cyber-exposure-insurance/Content/SLARemediation.htm).
- **Qualys VMDR**: TruRisk asset score 0–1000 + per-detection QDS; patch correlation ("which patch fixes which QIDs").

Definition norms for **patch compliance %** (from Automox/Intune/Action1 practice, https://www.automox.com/blog/it-and-compliance-reporting):
- *Eligible assets* = in-scope, active devices with supported OS, enrolled in scanning; **exclude** decommissioned devices, devices offline > 30 days, and documented exceptions (but count exceptions separately).
- Compliance is measured **within a per-severity SLA window** (e.g., critical patches applied within 14 days of release/detection), not as raw "no missing patches."
- Show both a point-in-time view ("as of date") and a rolling view (last 30 days) for trend/SLA adherence.
- Two granularities: per-device (device compliant iff no overdue patches) and per-patch (deployment success rate).

### 1.2 Meaning (b): configuration / framework compliance

Definition in practice: automated checks of device/server configuration against a **named policy** (CIS Benchmark, DISA STIG, PCI, internal standard), each check yielding pass/fail + severity + remediation instructions, rolled up to a compliance %, mapped to framework controls, exported as auditor-ready reports.

What the two direct competitors ship (this is the concrete bar for a ZenPlus "Compliance" tab):

- **SolarWinds Network Configuration Manager (NCM)**: out-of-the-box compliance assessments/reports for **DISA STIG, NIST FISMA, HIPAA, PCI DSS**; per-rule violation reports; **automated remediation scripts** to correct violations; vulnerability detection on device OS versions; real-time config-change monitoring feeding compliance state. (https://www.solarwinds.com/network-configuration-manager, datasheet https://www.solarwinds.com/assets/solarwinds/swdcv2/licensed-products/network-configuration-manager/resources/datasheets/ncm-datasheet.pdf)
- **ManageEngine Network Configuration Manager**: default policies for **CIS, SOX, HIPAA, PCI DSS** + custom policies; **compliance check runs automatically on every config backup**; violations carry severity, rule criteria, and remediation description; remediation via **configlets** (executable config templates) launched directly from the compliance report; instant alerts on violation; scheduled PDF/email compliance reports; RBAC on who can remediate. (https://www.manageengine.com/network-configuration-manager/compliance-and-automation.html, https://www.manageengine.com/network-configuration-manager/network-compliance-reporting-audit.html)

Distilled buyer expectations for the framework-compliance surface:
1. **OOTB policy library** with named standards (CIS/STIG/PCI at minimum) + custom-rule builder (regex/absence/presence of config lines for network devices; setting checks for servers).
2. **Per-device, per-rule pass/fail** with severity, the observed value vs. expected value, and remediation text (or an executable fix).
3. **Compliance % score** per policy, per device group, per device — with trend over time.
4. **Automatic re-check** on config change/backup, with alerting on new violations.
5. **Auditor-ready scheduled reports** (PDF/CSV), plus an **exception/waiver workflow** with expiry dates and justification (auditors specifically look for a "risk acceptance log" — see §1.4).

### 1.3 How products map findings to framework controls

The universal architecture is **two-layer indirection**: technical checks are written once, then mapped many-to-many onto framework controls. Reporting picks a framework and rolls check results up through the mapping.

- **Qualys Policy Compliance — "Mandate-Based Reporting"** is the canonical pattern: each internal control is mapped to granular *control objectives*, which are mapped to *mandates*; one scan produces compliance posture against **PCI DSS 4.0, HIPAA, NIST 800-53, ISO 27001, GDPR, FedRAMP, SOX — 90+ frameworks** from a single control library of 900+ policies covering CIS Benchmarks and DISA STIGs. (https://notifications.qualys.com/product/2021/12/17/control-mappings-for-mandate-based-reporting, https://blog.qualys.com/product-tech/2025/04/24/introducing-qualys-policy-audit-the-new-standard-for-audit-readiness)
- **Tenable**: 450+ downloadable **audit files** (Nessus `.audit` compliance checks) per OS/app/DB/network-device; Security Center ships **Assurance Report Cards (ARCs)** that compare current posture to a target ("demonstrate compliance roadmap") and **20+ NIST-CSF-specific dashboards**; claims automation of "more than 90% of CSF technical controls." (https://www.tenable.com/solutions/security-frameworks, https://www.tenable.com/solutions/nist-cybersecurity-framework)
- **CIS-CAT Pro Assessor v4** is the reference implementation for benchmark assessment: consumes **SCAP 1.2 data-stream collections, XCCDF 1.2 benchmarks, OVAL definition files**; emits HTML/XML results with a pass/fail per item, **numeric compliance score per item and per section**, observed vs. expected values; pairs with CIS-CAT Pro Dashboard for compliance-over-time. (https://ciscat-assessor.docs.cisecurity.org/en/latest/Coverage%20Guide/)
- Mapping sources you can reuse rather than author: CIS publishes benchmark→CIS-Controls-v8.1 mappings; CIS Controls map onward to NIST CSF 2.0, 800-53r5, ISO 27001:2022, PCI DSS 4.0 (CIS "mappings and compliance" downloads); NIST publishes CSF 2.0↔800-53r5 crosswalks (informative references in the CSF 2.0 CPRT tool, https://csrc.nist.gov/projects/cprt).

**ZenPlus implication:** model `check → control_objective → framework_control` as data (seeded from zentryc.com), not code. One checks table; framework rollups are joins. This is exactly what lets a mid-market product claim "PCI + HIPAA + ISO + CIS reporting" without writing four engines.

### 1.4 Framework cheat-sheet (what each one demands of this module, Aug 2026 status)

| Framework | Status (Aug 2026) | What the module must produce |
|---|---|---|
| **CIS Benchmarks** | 100+ benchmarks, Level 1/Level 2 profiles; v8.1 Controls | Per-benchmark compliance % (pass/fail per recommendation, section scores), CIS-CAT-style observed-vs-expected detail |
| **DISA STIG** | STIG Viewer 3.x; CKL (XML) legacy + CKLB (JSON) current | CAT I/II/III severity buckets; checklist export (.ckl/.cklb) for eMASS/GRC import; one published tool's risk roll-up: `(CATI×10)+(CATII×5)+(CATIII×1)` |
| **PCI DSS 4.0.1** | v4.0 mandatory since 31 Mar 2024; future-dated reqs enforced since 31 Mar 2025 | 6.3.1 risk-ranking process; 6.3.3 critical/high patches ≤ 1 month; 11.3.1 quarterly internal authenticated scans, rescan-to-clean for high+; 11.3.2 quarterly external ASV, all 4 quarters passing, medium+ must be remediated for a pass |
| **NIST CSF 2.0** | Final 26 Feb 2024; 6 functions / 22 categories / 106 subcategories | Map features to ID.AM (inventory), ID.RA (risk assessment, vuln identification), PR.PS (platform security/patching), DE.CM (monitoring); per-function coverage dashboard |
| **NIST 800-53 r5** | Current | RA-5 (vulnerability monitoring & scanning), SI-2 (flaw remediation), SI-5 (security alerts/advisories), CM-6 (config settings), CM-8 (component inventory) |
| **ISO 27001:2022** | Current; 93 Annex A controls | **A.8.8 Management of technical vulnerabilities** — see auditor-evidence list below |
| **HIPAA Security Rule** | Current rule is flexibility-based; **NPRM of 6 Jan 2025 not final** (OMB target moved to Jul 2027) | NPRM proposes: **patch critical vulns within 15 calendar days** (of identification or patch availability), high within 30 days; **vulnerability scans ≥ every 6 months; pen test ≥ annually**. Ship as an optional "HIPAA (proposed)" policy pack, labeled proposed. (https://www.hhs.gov/hipaa/for-professionals/security/hipaa-security-rule-nprm/factsheet/index.html) |
| **ACSC Essential Eight** | Nov 2023 maturity model current | Patch timelines (see §5); "patch applications"/"patch OS" are 2 of the 8 strategies; asset scanning cadence requirements |
| **EU NIS2** | In force; transposition was due 17 Oct 2024 (several states late — Germany Dec 2025); Jan 2026 amendment package proposed, adoption expected late 2026/2027 | Art. 21 risk-management measures include vulnerability handling & disclosure; customers need evidence of a working vuln-management process + incident reporting readiness |
| **EU CRA** | In force 10 Dec 2024; **reporting of actively exploited vulns mandatory from 11 Sep 2026** (24 h early warning); main obligations 11 Dec 2027; 5-year security-support requirement | Two angles: (1) customers' products need SBOM/vuln handling; (2) **ZenPlus itself, sold in the EU, becomes a "product with digital elements" — the vendor pipeline at zentryc.com doubles as ZenPlus's own CRA vulnerability-handling machinery** |

**What ISO/SOC-type auditors actually ask for on vulnerability management (A.8.8 checklists,** https://hightable.io/iso-27001-annex-a-8-8-audit-checklist/ **):**
1. Approved **vulnerability/patch management policy with explicit remediation SLAs** (e.g., "critical within 14 days").
2. **Scan configuration evidence** showing full asset-scope inclusion (IP ranges/tags), and **12 months of scan history** at consistent intervals.
3. Vulnerability reports **categorized by severity**.
4. An active **risk-acceptance / exception log** for deferred items (with owner, justification, expiry).
5. **Patch/remediation records** linked to findings, plus rescan ("before/after") evidence.
6. **Asset coverage documentation** — what is in scope and what is deliberately out.
The bar is *"managed deliberately and defensibly,"* not zero vulnerabilities. PCI adds: keep all four quarterly scan reports per year; failed-then-passing rescan chains are themselves evidence.

**ZenPlus implication:** the module must *retain and export* scan history ≥ 12 months, and exceptions must be first-class rows (asset, finding, justification, approver, expiry, auto-reactivation on expiry) — this is asked for by name in audits.

---

## 2. Risk scoring

### 2.1 The four standard inputs

**CVSS v3.1 / v4.0** (impact severity, static)
- v4.0 published 1 Nov 2023 (FIRST). Renames Temporal→**Threat** metrics; introduces nomenclature **CVSS-B / CVSS-BT / CVSS-BE / CVSS-BTE** — you are supposed to label which groups a displayed number includes. Threat metrics (Exploit Maturity) *reduce* the reasonable-worst-case base score when no exploitation evidence exists. (https://www.first.org/cvss/v4.0/specification-document, user guide https://www.first.org/cvss/v4.0/user-guide)
- Qualitative bands (same for v3.1 and v4.0): None 0.0 / Low 0.1–3.9 / Medium 4.0–6.9 / High 7.0–8.9 / **Critical 9.0–10.0**.
- Reality: most CVEs still carry v3.1 only; v4.0 coverage is growing but partial. Store both; prefer v4.0-BT when present, else v3.1 base.

**EPSS** (exploitation likelihood, dynamic)
- FIRST's ML model: **probability (0–1) that a CVE will be exploited in the wild within the next 30 days**, plus a percentile rank across all CVEs. **Updated daily for every CVE.** Current model: **EPSS v4 (released 17 Mar 2025)**. (https://www.first.org/epss/)
- API: `https://api.first.org/data/v1/epss?cve=CVE-XXXX-YYYY` (also bulk daily CSV `https://epss.empiricalsecurity.com/epss_scores-YYYY-MM-DD.csv.gz`). Free, no key.
- Distribution is heavily right-skewed (median score ≈ 0.01–0.04): raw multiplication crushes scores; use **bands/percentiles**. Practitioner consensus thresholds: **≥ 0.5 (or ≥ 95th pct) = treat as imminent; 0.1–0.5 = elevated, patch this cycle; < 0.01 & no exploit = deprioritize**. Common single cutoff seen in guidance: EPSS > 0.1 for "high priority." FIRST's own guidance: use CVSS as severity floor, EPSS to rank order within tiers.

**CISA KEV** (confirmed exploitation, authoritative)
- Catalog of vulnerabilities with *evidence of active exploitation*. **1,282 entries as of 14 Aug 2026, 234 flagged as used in ransomware campaigns.**
- JSON feed: `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json`. Fields: `cveID, vendorProject, product, vulnerabilityName, dateAdded, shortDescription, requiredAction, dueDate, knownRansomwareCampaignUse (Known/Unknown), notes, cwes`.
- `dueDate` = federal remediation deadline (BOD-driven; historically ~2–3 weeks after `dateAdded`). Under BOD 26-04 the KEV flag becomes one of four matrix variables rather than the sole trigger, but KEV membership remains the single strongest public exploitation signal, and `knownRansomwareCampaignUse` is a free ransomware-risk feature.

**Asset criticality & exposure** (local context — the input only the customer has)
- Qualys ACS: analyst-assigned 1–5. Tenable ACR: 0–10 (auto-derived + override). Every serious product lets admins tag criticality per asset/group and marks **internet-exposed** assets separately (BOD 26-04 makes "publicly exposed" the customer-answered variable).
- ZenPlus already has device tags + device types; criticality can default from device class (core switch/firewall/server running agent = high; access switch = medium; workstation = normal) with per-device override.

### 2.2 Published composite-scoring approaches

**Tenable VPR** (https://www.tenable.com/blog/what-is-vpr-and-how-is-it-different-from-cvss, https://docs.tenable.com/vulnerability-management/Content/Explore/Findings/RiskMetrics.htm)
- 0.1–10.0, same bands as CVSS (Critical 9.0–10.0, High 7.0–8.9, Medium 4.0–6.9, Low 0.1–3.9). Recomputed **daily** for 280k+ CVEs.
- Structure: **VPR = f(technical impact, threat)** where technical impact = CVSSv3 impact subscore and threat = ML blend of drivers: vulnerability age, exploit code maturity (Unproven/PoC/Functional/High), product coverage, **exploit probability (%), CISA KEV membership, malware-observation intensity & recency (last 30 days), "in the news" intensity & recency, targeted industries/regions** (driver list from the 2026 VPR revision). Opaque ML — the thing mid-market buyers complain about.

**Qualys TruRisk** (https://blog.qualys.com/vulnerabilities-threat-research/2022/12/16/implement-risk-based-vulnerability-management-with-qualys-trurisk-part-2, https://docs.qualys.com/en/csam/latest/inventory/trurisk_score.htm)
- Per-detection **QDS 1–100** (CVSS + real-world signals: exploit maturity, malware/ransomware association, threat-actor use, dark-web chatter, KEV).
- Per-asset **TruRisk 0–1000**, published formula:
  `TruRisk = MIN( ACS × Σ_severity [ w_s × Avg(QDS_s) × Count(QDS_s)^(1/100) ], 1000 )` with ACS 1–5 and per-severity weights w_c > w_h > w_m > w_l. Count dampening via the 1/100 power keeps "many lows" from beating "one critical."
- Asset bands: **Severe 850–1000, High 700–849, Medium 500–699, Low 0–499.**

**Rapid7 Active Risk** (https://www.rapid7.com/blog/post/2023/09/25/introducing-active-risk/)
- **0–1000** per vulnerability. Latest available CVSS (v3.1 preferred) compounded with exploit/malware intelligence: **AttackerKB, Metasploit modules, ExploitDB, Project Lorelei honeypots, CISA KEV, EPSS**, dark-web sources. Rationale for the 0–1000 scale: thousands of CVEs are CVSS 10.0; wider scale restores ordering. Same score across VM and cloud modules ("consistency across surfaces" is a selling point).

**SSVC** (CISA/CMU-SEI decision trees, https://www.cisa.gov/stakeholder-specific-vulnerability-categorization-ssvc)
- Not a score — a **decision tree** over (Exploitation status, Automatable, Technical impact, Mission & well-being prevalence) yielding an action: **Track / Track\* / Attend / Act**. CISA publishes a calculator and uses SSVC internally to decide KEV additions. Value for ZenPlus: the *vocabulary* (automatable, total vs. partial control) and the idea that output should be an **action bucket with a deadline**, not just a number.

**CISA BOD 26-04** (replaces BOD 22-01; see §5 for timelines) is effectively **SSVC operationalized as policy**: four binary variables → 16 combinations → 5 timeline tiers. CISA supplies three of the four variable answers per CVE through **Vulnrichment**; the customer answers only "is the asset publicly exposed." This is the most modern template for a *transparent* matrix and is what federal-adjacent buyers will ask about in 2026–27.

### 2.3 The vulnerability-data ecosystem in 2026 (constraints on the zentryc.com feed)

- **NVD triage (15 Apr 2026):** NIST now fully enriches only KEV-listed CVEs, federal-use software, and EO-14028-critical software (~15–20% of new volume); everything else gets "Lowest Priority" (no CVSS, no CPE mapping, no CWE); ~29,000 backlog CVEs reclassified "Not Scheduled." Driven by a 263% CVE-volume surge 2020→2025. (https://www.nist.gov/news-events/news/2026/04/nist-updates-nvd-operations-address-record-cve-growth, https://www.infosecurity-magazine.com/news/nvd-enrichment-premarch-2026/)
- **CISA Vulnrichment:** SSVC decision values, CWE, CVSS embedded in the **ADP container of CVE JSON 5.x records** on CVE.org (GitHub `cisagov/vulnrichment`, and in `CVEProject/cvelistV5`). This — not NVD — is now the primary free enrichment source.
- **Alternatives:** VulnCheck NVD++ (free community CPE/CVSS enrichment), Google OSV (open-source packages).
- **Vendor PSIRT sources for network gear** (the SNMP-polled half of ZenPlus):
  - **Cisco PSIRT openVuln API** — REST; query advisories by CVE, advisory ID, product, or **exact OS version** (`/OSType/{ostype}?version=` covering ASA, FMC, FTD, FXOS, IOS, IOS XE, NX-OS incl. ACI mode); integrates the Software Checker logic; CVRF/OVAL/CVSS formats; free with Cisco DevNet registration. (https://developer.cisco.com/docs/psirt/)
  - Juniper (JSA advisories), Fortinet (FortiGuard PSIRT feed), Palo Alto (security.paloaltonetworks.com API), Aruba/HPE, MikroTik (CVE feed only). Version-string→advisory matching per vendor beats generic CPE matching for network OSes, because NVD CPE data for network gear is now sparse (see triage above).
- **EOL data:** `endoflife.date` — community-maintained, covers 300+ products (network OSes incl. Cisco IOS-XE trains, Windows/Windows Server, distros, DBs); **API v1: https://endoflife.date/docs/api/v1/** (beta; breaking changes possible). Lansweeper is the buyer-familiar reference for EOL/EOS dashboards driven from inventory + manufacturer dates; its pattern: EOL dashboards auto-update as inventory changes, per-product audit reports (Windows Server, Win 10, Office, SQL Server, .NET). (https://www.lansweeper.com/solutions/use-cases/asset-lifecycle-management/)

**Feed design implication:** zentryc.com should ship appliances a *pre-joined* record per CVE: affected product/version ranges (vendor-advisory-derived for network OSes, CPE-derived for server software), CVSS v3.1 + v4.0 (with source), **EPSS score + percentile (refreshed daily)**, KEV flags (`in_kev, date_added, due_date, ransomware`), exploit-maturity tag, Vulnrichment SSVC values (`automatable`, `technical_impact`), and EOL dates per product. Appliances then do matching + scoring locally against inventory (SNMP sysDescr/version for devices, agent software inventory for servers) — no NVD dependency at runtime.

### 2.4 Recommended ZenPlus scoring model (concrete)

Design constraints: transparent (auditor/operator can recompute by hand), monotonic, uses only feed fields above, degrades gracefully when EPSS/CVSS are missing (post-NVD-triage reality), and mirrors the market's two-level shape (per-finding 0–100, per-asset 0–1000).

**Level 1 — Finding Risk Score (FRS, 0–100), risk = impact × likelihood × exposure:**

```
FRS = min(100, 100 × I × T × E)

I  (impact, 0–1)    = CVSS_effective / 10
                      CVSS_effective = CVSSv4-BT if present, else CVSSv3.1 base,
                      else severity fallback: vendor-advisory severity mapped
                      Critical→9.5, High→8.0, Medium→5.5, Low→3.0, unknown→6.0

T  (threat, 0–1)    = ladder (take the highest matching rung):
                      1.00  KEV and knownRansomwareCampaignUse = Known
                      0.90  KEV
                      0.80  weaponized exploit public (Metasploit/functional) OR EPSS ≥ 0.50
                      0.60  PoC public OR EPSS 0.10–0.50
                      0.40  EPSS 0.01–0.10
                      0.25  EPSS < 0.01 or unscored, no known exploit

E  (exposure)       = 1.15 internet-facing asset
                      1.00 internal
                      0.85 isolated/management-VLAN-only (admin-tagged)
```

FRS bands: **Critical ≥ 70, High 45–69, Medium 20–44, Low < 20.**

Worked examples (sanity checks against market behavior):
- CVSS 10.0, KEV, internet-facing firewall: 100 × 1.0 × 0.9 × 1.15 → **100 (Critical)**. ✔ tops the queue.
- CVSS 9.8, no exploit, EPSS 0.005, internal server: 100 × 0.98 × 0.25 × 1.0 = **24.5 (Medium)**. ✔ matches VPR/TruRisk behavior — most paper-criticals descend. **UI must always display CVSS severity alongside FRS** ("Critical severity / Medium risk") or buyers will distrust the number.
- CVSS 6.5, EPSS 0.92, internet-facing: 100 × 0.65 × 0.80 × 1.15 = **59.8 (High)** — correctly outranks the unexploited 9.8. ✔ the canonical EPSS teaching example.
- KEV + ransomware, CVSS 7.8, internal: 100 × 0.78 × 1.0 × 1.0 = **78 (Critical)**. ✔ ransomware KEVs never sit below Critical on any asset class.

Rationale for choices: multiplicative form is the textbook risk definition and is what makes the score *explainable in one tooltip line* ("9.8 impact × 0.25 threat × 1.0 exposure = 24"); the T-ladder (instead of raw EPSS multiplication) fixes EPSS skew and encodes the industry consensus ordering **KEV+ransomware > KEV > weaponized/EPSS-high > PoC > theoretical**; the E factor implements BOD 26-04's "publicly exposed" variable with the only two answers a mid-market customer can reliably give; caps/floors keep every rung's contribution visible. Weights are data (per-tenant policy table), defaults as above.

**Level 2 — Asset Risk Score (ARS, 0–1000), Qualys-shaped but fully published:**

```
ARS = min(1000, K(ACS) × ( 100·√n_C + 40·√n_H + 10·√n_M + 2·√n_L ))

n_s     = count of open findings in FRS band s on the asset
K(ACS)  = 0.5 / 0.75 / 1.0 / 1.25 / 1.5 for asset criticality 1–5
          (default 3; auto-suggest 4–5 for core/firewall/server device classes)
```

Bands (align with Qualys so buyers' mental model transfers): **Severe ≥ 850, High 700–849, Medium 500–699, Low < 500.** √-dampening keeps 100 lows (2·10=20 pts) below one critical (100 pts); K keeps a criticality-5 asset with one critical (150) above a criticality-1 asset with four (100·2·0.5=100).

**Optional overlay (phase 2): SSVC/BOD 26-04 action buckets.** Since the feed carries Vulnrichment `automatable` + `technical_impact` and the asset carries `internet-facing`, ZenPlus can label each finding **Act (3d) / Attend (14d) / Track (60d) / Defer** per the BOD 26-04 matrix — a strong differentiator for public-sector-adjacent deals, and it is pure lookup, no new scoring.

---

## 3. Dashboard metrics / KPIs

### 3.1 The canonical KPI set (definition + formula + benchmark)

Sources: SentinelOne 20-KPI guide (https://www.sentinelone.com/cybersecurity-101/cybersecurity/vulnerability-management-metrics/), Tenable MTTR guide (https://www.tenable.com/cybersecurity-guide/learn/mean-time-to-remediate-mttr), Greenbone KPI guide, Automox patch-compliance dashboard guide, appsecsanta metrics guide.

| KPI | Definition / formula | Benchmark / target norms |
|---|---|---|
| **Org & per-asset risk score trend** | ARS aggregate (sum or mean of top-N) plotted daily/weekly; the headline chart | Direction matters more than level; boards want 90-day trend |
| **MTTR by severity** | mean (and median — report both; mean is outlier-skewed) of `closed_at − detected_at` for findings closed in period, split by FRS band | Targets: **Critical < 7 days (mature < 3), High < 30, Medium < 90**. Industry reality: median ~43 days to full resolution (2025, CISA-cited); only 26% of KEV vulns fully remediated in 2025 |
| **SLA compliance rate** | findings remediated within SLA ÷ findings that came due in period | Healthy programs ≥ 90%; auditors ask for the misses + exceptions |
| **SLA aging buckets** | open findings grouped by time-past-due: **due in ≤7d / overdue 1–30d / 31–60d / 61–90d / 90+d** | The "90+ overdue criticals" cell is the audit red flag; show count + oldest |
| **Patch compliance %** | compliant eligible devices ÷ eligible devices (see §1.1 eligibility/exclusion rules); per-severity window | Per-device and per-patch views; point-in-time + rolling 30d |
| **KEV exposure** | count of open findings on KEV CVEs; sub-metric: KEV **past federal due date**; sub-metric: ransomware-KEV count | Norm is a dedicated tile; target 0 past-due |
| **EOL assets** | count of assets running EOL software/OS **now**, and approaching within 90/180/365 days (Lansweeper pattern) | Tile + drill-down per product; feeds framework checks (PCI 6.3.x, E8) |
| **Top risky assets** | top-10 by ARS with sparkline and dominant finding | Every leading product's landing widget |
| **New this week (deltas)** | new findings, new KEV matches on existing inventory, newly-EOL software, closed count — vs. prior period | Powers the delta report (§4.2) |
| **Fix rate (backlog burn)** | findings closed ÷ findings opened per period | < 1.0 = growing backlog; top teams **1.2–1.5** |
| **Remediation coverage** | remediated ÷ total detected (cumulative), overall and criticals-only | Criticals-only version is the one execs read |
| **Scan/agent coverage** | assets assessed in last N days ÷ inventory | The auditor's first question; target ≥ 95% |
| **Exception count** | open risk acceptances, with soonest expiry | Must be visible, not buried — auditors reconcile it |
| **Recurrence rate** | findings reopened after closure ÷ closures | Signals bad patching or config drift |

### 3.2 What auditors ask the dashboard/reporting to prove

From §1.4 evidence lists, restated as product requirements: 12-month scan history retention; severity categorization on every finding; SLA policy visible in-product and referenced in reports; exception log export; before/after rescan evidence per remediation; asset-scope documentation (what's excluded and why). PCI adds quarterly cadence proof (four passing internal + four passing ASV reports per year, rescan chains for fails).

---

## 4. Reporting norms

### 4.1 Executive summary (PDF)

Norms (Brinqa board-reporting guide https://www.brinqa.com/blog/vulnerability-management-reporting-to-board, NopSec https://www.nopsec.com/blog/how-to-create-vulnerability-management-reports-for-executives/):
- 1–2 pages, **business-aligned, few metrics**: overall risk trend (90d), SLA compliance %, MTTR by severity vs. target, KEV/ransomware exposure, top-5 risky assets/business groups, framework-compliance bar per active framework, and "what changed since last report."
- Anti-pattern named repeatedly: CVE/asset dumps in the exec report. Detail belongs in appendices/CSV.
- "Before/after" remediation evidence (closed criticals, risk reduced) is the persuasion device — always include a *risk reduced this period* figure.
- Role-split norm: executive PDF / operator queue / auditor evidence pack are three distinct outputs of one engine.

### 4.2 Delta reports

Standard in Nessus/Qualys/SC: **new / fixed / unchanged** since (a) previous scan and (b) previous period. Norm fields: new findings (with risk), resolved findings (with closure proof), net risk change, new KEV matches, newly-EOL. Weekly scheduled email delta is the most-used report in operations; make it default-on.

### 4.3 Per-framework compliance reporting

- Headline: **compliance % per framework** = passed checks ÷ applicable checks (excluding accepted-risk items but *listing* them), with trend. Per-section/control-family drill-down (CIS-CAT emits per-section scores; Qualys mandate reports roll to control objectives).
- Per-device compliance % + per-rule detail (observed vs. expected + remediation text) — the SolarWinds/ManageEngine NCM report shape buyers already know.
- ARC-style "target vs. actual" (Tenable): let admins set a target % per framework and show the gap.

### 4.4 Evidence export formats

| Format | Use | Notes |
|---|---|---|
| **PDF** | exec + auditor summaries | branded, scheduled email |
| **CSV/XLSX** | finding-level and check-level exports | the universal auditor request; include detected/closed dates, severity, CVSS, FRS, SLA due, status, exception ref |
| **JSON (API)** | GRC/SIEM integration | full finding + check objects |
| **CKL / CKLB** | DoD/eMASS STIG checklists | CKL = XML (legacy STIG Viewer 2), CKLB = JSON (STIG Viewer 3+); needed only if pursuing federal/defense; conversion between them is lossless for status/details/comments |
| **XCCDF/ARF results** | SCAP ecosystems | emit XCCDF result XML per benchmark run; positions ZenPlus with CIS-CAT-familiar shops |
| **OSCAL assessment-results** | forward-looking | FedRAMP mandates machine-readable (OSCAL) authorization data from **30 Sep 2026**; not a mid-market must-have yet — note as roadmap only (https://www.ignyteplatform.com/blog/fedramp/oscal-and-fedramp-automation/) |

Scheduling/distribution norms: cron-scheduled, RBAC-scoped recipients, report archive retained ≥ 12 months in-product (audit evidence), every dashboard tile drills to an exportable list.

---

## 5. SLA norms worth encoding as default remediation policies

### 5.1 Regulatory timelines (the anchors)

**CISA BOD 22-01** (Nov 2021 — **revoked mid-2026**, but still the timeline every buyer knows): KEV CVEs with CVE-ID assigned ≥ 2021 → **2 weeks**; pre-2021 CVE-IDs → **6 months**; thereafter per-entry `dueDate` in the catalog. (https://www.cisa.gov/news-events/directives/bod-22-01-reducing-significant-risk-known-exploited-vulnerabilities-revoked)

**CISA BOD 26-04** (current; policies updated immediately, processes ~Aug 2026, full timelines by ~7 Dec 2026; federal-mandatory, private-sector-influential): four binary variables — **publicly exposed** (agency-answered), **in KEV**, **automatable**, **technical impact total vs. partial** (last three published per-CVE via Vulnrichment) — mapped over 16 combinations to five tiers (https://www.cisa.gov/news-events/directives/bod-26-04-prioritizing-security-updates-based-risk, Tenable FAQ https://www.tenable.com/blog/cisa-bod-26-04-FAQ-vulnerability-remediation-impact):

| Tier | Condition (summary) | Deadline |
|---|---|---|
| 3 days **+ forensic triage** (compromise check) | KEV **and** total control | 3 calendar days |
| 3 days | e.g., publicly exposed + automatable + total control (even pre-KEV) | 3 calendar days |
| 14 days | most other KEV combinations; several high-risk non-KEV combos | 14 days |
| 60 days | lower-risk combos (e.g., not exposed, automatable, partial control) | 60 days |
| Defer | none of the four criteria | next scheduled system upgrade |

Timelines are **dynamic**: KEV addition or exposure change re-derives the due date. Context CISA cited: only 26% of KEV vulns fully remediated in 2025 (down from 38%); median full-resolution time 43 days; AI shrinking disclosure-to-exploit windows. At one large agency ~1% of instances landed in the 3-day tier and >60% qualified for deferral — i.e., the matrix *concentrates* urgency, it doesn't inflate it.

**PCI DSS 4.0.1**: critical/high patches (per the entity's 6.3.1 risk ranking) installed **within one month of release** (6.3.3); other patches within an entity-defined window (~3 months customary); internal authenticated scans **at least every 3 months** + after significant change, **rescan until all high/critical resolved** (11.3.1.x); external **ASV scans quarterly**, passing = no medium+ findings, **all four quarters must pass**, rescan-to-pass (11.3.2.x).

**ACSC Essential Eight** (Nov 2023 model, patch-applications / patch-OS strategies): **internet-facing services: 48 hours if an exploit exists, otherwise 2 weeks — at every maturity level**; workstation/internal apps & OS: **1 month at ML1, 2 weeks at ML2/ML3**; ML3 tightens high-risk app categories (browsers, office suites, PDF, extensions) to **48 hours**. Also mandates scanner cadence (daily for internet-facing services). (https://learn.microsoft.com/en-us/compliance/anz/e8-patch-app, https://blueprint.asd.gov.au/security-and-governance/essential-eight/patch-os/)

**HIPAA Security Rule NPRM** (proposed 6 Jan 2025; **not final** — OMB target Jul 2027): patch **critical within 15 calendar days** (of identifying need or patch availability), **high within 30 days**; vulnerability scans ≥ every 6 months; annual pen test. Ship as a policy pack labeled "(proposed)". (https://www.hhs.gov/hipaa/for-professionals/security/hipaa-security-rule-nprm/factsheet/index.html)

**Common internal SLA tiers** (industry survey of guides, e.g. https://www.secure.com/blog/vulnerability-remediation-slas): most-cited default ladder **Critical 15 days (aggressive shops 24–72 h or 7 d) / High 30 d / Medium 60–90 d / Low 90–180 d**; regulated verticals compress (PCI shops: 30 d for critical+high; FedRAMP has its own POA&M clocks). Trend: exposure-based SLAs (criticality + exploitability) instead of raw severity — which is exactly what FRS-band-based SLAs give.

### 5.2 Recommended ZenPlus default policy packs

Encode SLA policies as data: `(matcher → days_to_due)`, matchers over {KEV flags, FRS band, CVSS band, exposure, framework pack}. First match wins, KEV rules always outrank band rules.

**Pack: ZenPlus Default** (mid-market, defensible, maps to ISO A.8.8 expectations):

| Rule | Due |
|---|---|
| KEV + ransomware, or KEV on internet-facing asset | **7 days** |
| KEV (any asset) | **14 days** (BOD 22-01 heritage; universally recognized) |
| FRS Critical (≥ 70) | **15 days** |
| FRS High (45–69) | **30 days** |
| FRS Medium (20–44) | **90 days** |
| FRS Low (< 20) | **180 days** (or "next maintenance window") |
| EOL software/OS | 90 days to upgrade plan, tracked as its own finding type |

**Pack: PCI DSS** — critical/high (entity ranking) 30 days; medium 90; rescan-to-clean enforcement on closure; quarterly scan-cadence monitor. **Pack: Federal / BOD 26-04** — the 5-tier matrix verbatim (needs Vulnrichment fields in feed + exposure tag). **Pack: Essential Eight ML2/ML3** — 48 h exploited internet-facing / 2 weeks / 1 month ladder. **Pack: HIPAA (proposed)** — 15/30-day ladder + 6-month scan cadence + annual pen-test reminder.

Policy-engine requirements implied: per-pack per-tenant overrides; due-date recomputation on KEV addition/exposure change (BOD 26-04 dynamism); grace/exception workflow with expiry; SLA clock starts at `detected_at` (first observation on the asset), pauses never, closes on verified rescan.

---

## 6. Source URL index

**Risk scoring / data sources**
- FIRST EPSS: https://www.first.org/epss/ — API https://api.first.org/data/v1/epss
- CVSS v4.0 spec: https://www.first.org/cvss/v4.0/specification-document — user guide: https://www.first.org/cvss/v4.0/user-guide
- CISA KEV: https://www.cisa.gov/known-exploited-vulnerabilities-catalog — JSON feed https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
- KEV stats Aug 2026 (1,282 / 234 ransomware): https://senserva.com/non-microsoft-cve-tracker.html — analysis https://www.runzero.com/resources/kevology/
- Tenable VPR: https://www.tenable.com/blog/what-is-vpr-and-how-is-it-different-from-cvss — drivers https://docs.tenable.com/vulnerability-management/Content/Explore/Findings/RiskMetrics.htm — scoring PDF https://docs.tenable.com/quick-reference/scoring-explained/Content/PDF/tenable-scoring-explained.pdf
- Qualys TruRisk formula: https://blog.qualys.com/vulnerabilities-threat-research/2022/12/16/implement-risk-based-vulnerability-management-with-qualys-trurisk-part-2 — bands https://docs.qualys.com/en/csam/latest/inventory/trurisk_score.htm
- Rapid7 Active Risk: https://www.rapid7.com/blog/post/2023/09/25/introducing-active-risk/
- SSVC: https://www.cisa.gov/stakeholder-specific-vulnerability-categorization-ssvc — guide PDF https://www.cisa.gov/sites/default/files/publications/cisa-ssvc-guide%20508c.pdf
- Simple weighted formula example: https://docs.cybersecfeed.com/docs/guides/guides-risk-based-prioritization — EPSS/CVSS/KEV combination: https://www.arnica.io/blog/leveraging-epss-cvss-and-kev-for-comprehensive-risk-management

**Directives / regulations**
- BOD 22-01 (revoked): https://www.cisa.gov/news-events/directives/bod-22-01-reducing-significant-risk-known-exploited-vulnerabilities-revoked
- BOD 26-04: https://www.cisa.gov/news-events/directives/bod-26-04-prioritizing-security-updates-based-risk — implementation guidance https://www.cisa.gov/news-events/directives/bod-26-04-implementation-guidance-prioritizing-security-updates-based-risk — Tenable FAQ https://www.tenable.com/blog/cisa-bod-26-04-FAQ-vulnerability-remediation-impact — VulnCheck https://www.vulncheck.com/blog/cisa-bod-26-04
- PCI DSS 4.x patching/scanning: https://www.foregenix.com/blog/pci-dss-v4.0-asv-scanning-guide-for-compliance — https://www.securecodinghub.com/blog/pci-dss-vulnerability-scanning-asv-and-internal-scans — https://pcidssguide.com/pci-dss-requirement-6/
- HIPAA NPRM: https://www.hhs.gov/hipaa/for-professionals/security/hipaa-security-rule-nprm/factsheet/index.html — status https://www.barradvisory.com/resource/the-hipaa-security-rule-key-updates-since-march-2025/
- Essential Eight: https://learn.microsoft.com/en-us/compliance/anz/e8-patch-app — https://blueprint.asd.gov.au/security-and-governance/essential-eight/patch-os/
- NIS2: https://digital-strategy.ec.europa.eu/en/policies/nis2-directive — CRA: https://digital-strategy.ec.europa.eu/en/policies/cyber-resilience-act — 2026 update https://www.reedsmith.com/our-insights/blogs/viewpoints/102mnj2/eu-cybersecurity-regulatory-update-for-2026-and-beyond/
- ISO 27001 A.8.8 audit evidence: https://hightable.io/iso-27001-annex-a-8-8-audit-checklist/ — https://www.isms.online/iso-27001/annex-a-2022/8-8-management-of-technical-vulnerabilities-2022/
- NIST CSF 2.0 structure: https://www.resiplan.eu/en/blog/nist-csf-categories-table — CPRT mappings https://csrc.nist.gov/projects/cprt

**Ecosystem shifts**
- NVD triage Apr 2026: https://www.nist.gov/news-events/news/2026/04/nist-updates-nvd-operations-address-record-cve-growth — https://www.infosecurity-magazine.com/news/nvd-enrichment-premarch-2026/ — https://www.recordedfuture.com/blog/nist-nvd-enrichment — Tenable take https://www.tenable.com/blog/nvd-cuts-cve-enrichment-how-tenable-helps
- Cisco PSIRT openVuln API: https://developer.cisco.com/docs/psirt/ — https://sec.cloudapps.cisco.com/security/center/resources/openvulnapi
- endoflife.date API: https://endoflife.date/docs/api/v1/ — Lansweeper EOL pattern: https://www.lansweeper.com/solutions/use-cases/asset-lifecycle-management/

**Competitor compliance surfaces**
- SolarWinds NCM: https://www.solarwinds.com/network-configuration-manager — STIG page https://www.solarwinds.com/public-sector/disa-stig-compliance
- ManageEngine NCM compliance: https://www.manageengine.com/network-configuration-manager/compliance-and-automation.html — reporting https://www.manageengine.com/network-configuration-manager/network-compliance-reporting-audit.html
- ManageEngine VMP: https://www.manageengine.com/vulnerability-management/features.html — Patch compliance: https://www.manageengine.com/patch-management/patch-compliance.html
- Tenable frameworks/ARCs: https://www.tenable.com/solutions/security-frameworks — https://www.tenable.com/solutions/nist-cybersecurity-framework
- Qualys mandate reporting: https://notifications.qualys.com/product/2021/12/17/control-mappings-for-mandate-based-reporting — Policy Audit https://blog.qualys.com/product-tech/2025/04/24/introducing-qualys-policy-audit-the-new-standard-for-audit-readiness
- CIS-CAT Pro: https://ciscat-assessor.docs.cisecurity.org/en/latest/Coverage%20Guide/

**KPIs / reporting**
- SentinelOne 20 KPIs: https://www.sentinelone.com/cybersecurity-101/cybersecurity/vulnerability-management-metrics/
- Tenable MTTR: https://www.tenable.com/cybersecurity-guide/learn/mean-time-to-remediate-mttr — Praetorian MTTR: https://www.praetorian.com/security-101/mean-time-to-remediate-mttr/
- Automox patch-compliance dashboards: https://www.automox.com/blog/it-and-compliance-reporting
- Exec/board reporting: https://www.brinqa.com/blog/vulnerability-management-reporting-to-board — https://www.nopsec.com/blog/how-to-create-vulnerability-management-reports-for-executives/
- SLA guides: https://www.secure.com/blog/vulnerability-remediation-slas — https://anchorcybersecurity.com/blog/realistic-slas
- STIG formats: STIG Viewer 3 user guide https://dl.dod.cyber.mil/wp-content/uploads/stigs/pdf/U_STIG_Viewer_3-x_User_Guide_V1R5.pdf — CKL/CKLB https://stigworkbench.com/docs
- OSCAL/FedRAMP: https://www.ignyteplatform.com/blog/fedramp/oscal-and-fedramp-automation/ — https://quzara.com/fedramp/oscal
