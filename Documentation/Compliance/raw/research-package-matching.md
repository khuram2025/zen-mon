# Research: Matching Installed-Software Inventory, OS & Firmware Versions to CVEs and Patches

*Raw research package for the ZenPlus "Compliance & Vulnerability Management" module — web research current as of 2026-08-18.*

This report covers: (1) what inventory ZenPlus already has and what is missing; (2) Windows matching (MSRC API, KB supersedence, build numbers, third-party apps); (3) Linux matching (distro advisories as ground truth, the backport problem, how Grype/Trivy/Wazuh do it); (4) generic name+version→CPE matching and its failure modes; (5) network-gear firmware matching via vendor PSIRT/CSAF feeds; (6) patch mapping, recommended-version computation, and EOL interplay; (7) a recommended matching pipeline with confidence tiers for ZenPlus.

---

## 1. What ZenPlus already collects (grounding in the codebase)

The module does not start from zero — inventory tables and version extraction already exist:

### 1.1 Servers (Windows/Linux hosts)

`servers` table — `/opt/zenplus/scripts/migrate-016-server-monitoring.sql:13-39`:

```sql
CREATE TABLE IF NOT EXISTS servers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ...
    os_type             VARCHAR(20) NOT NULL DEFAULT 'unknown'
                        CHECK (os_type IN ('windows','linux','macos','bsd','other','unknown')),
    os_name             VARCHAR(255),
    os_version          VARCHAR(128),
    kernel_or_build     VARCHAR(128),
    architecture        VARCHAR(32),
    collection_mode     VARCHAR(20) NOT NULL DEFAULT 'agent'
                        CHECK (collection_mode IN ('agent','agentless_wmi','agentless_winrm','snmp','ssh','none')),
    ...
);
```

`server_software_inventory` — `/opt/zenplus/scripts/migrate-016-server-monitoring.sql:293-301` (re-declared in `migrate-030-server-monitoring.sql:263-271`):

```sql
CREATE TABLE IF NOT EXISTS server_software_inventory (
    server_id     UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    package_name  VARCHAR(255) NOT NULL,
    version       VARCHAR(128),
    vendor        VARCHAR(255),
    install_date  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, package_name)
);
```

Read paths: `/opt/zenplus/server/app/api/v1/servers.py:729,736` (`SELECT ... FROM server_software_inventory WHERE server_id = :id`). Agent binaries are distributed via the `agent_packages` table (`migrate-016-server-monitoring.sql:176`) and `/api/v1/agents/packages/{platform}/latest`.

**Gaps that the vulnerability module needs added to inventory collection:**

- No **architecture** per package, no **package-manager source** (`dpkg`/`rpm`/`apk`/registry-ARP/msi/winget) column, no **epoch/release** split for RPM — all load-bearing for correct matching (see §4.4).
- No **Windows hotfix/KB list** (nothing matches `hotfix|installed_kb|qfe` anywhere in `/opt/zenplus/server` or `/opt/zenplus/poller`). Windows OS CVE evaluation is impossible without either the installed-KB list (`Win32_QuickFixEngineering` / `Get-HotFix`) or the full OS build incl. UBR (`CurrentBuild` + `UBR` registry values → e.g. `10.0.19045.4291`).
- No **distro codename/release id** for Linux (`/etc/os-release` `ID`, `VERSION_ID`, `VERSION_CODENAME`) distinct from free-text `os_name`; distro feeds are keyed by exact release (e.g. `Ubuntu:22.04:LTS`, `bookworm`).
- No **source-package name** for dpkg/rpm packages (distro advisories are keyed by *source* package; binary→source mapping needed, see §4.5).

### 1.2 Network devices

`devices` table — base in `/opt/zenplus/scripts/init-postgres.sql:31-60`; SNMP identity columns added in `/opt/zenplus/scripts/migrate-004-snmp.sql:18-22`:

```sql
ADD COLUMN IF NOT EXISTS sys_object_id        VARCHAR(255),
ADD COLUMN IF NOT EXISTS vendor               VARCHAR(100),
ADD COLUMN IF NOT EXISTS model                VARCHAR(255),
ADD COLUMN IF NOT EXISTS os_version           VARCHAR(255),
ADD COLUMN IF NOT EXISTS profile_id           UUID;
```

Discovery already extracts `matched_vendor`, `matched_model`, `matched_os_version` from `sysDescr`/`sysObjectID` (`/opt/zenplus/scripts/migrate-005-snmp-discovery.sql:39-41`), and device profiles carry `sys_descr_regex` classifiers per vendor OS (`migrate-064-retire-legacy-template-stubs.sql:61-94` — FortiOS, PAN-OS, JUNOS, IOS/IOS-XE/NX-OS). Monitoring templates already poll firmware versions by OID, e.g. FortiGate `fgt_fw_version` OID `1.3.6.1.4.1.12356.101.4.1.1.0` (`migrate-062-monitoring-templates.sql:112`).

**Gap:** `os_version` is one free-text field; the module needs a *normalized* (vendor, os_family, version-train, version) tuple per device, plus model/PID for hardware EOL (Cisco EoX is keyed by product ID).

---

## 2. Windows: OS + Microsoft products

### 2.1 MSRC CVRF/CSAF API — the authoritative machine-readable source

- **CVRF API (JSON/XML, no auth key required since ~2021):**
  - Index of monthly documents: `https://api.msrc.microsoft.com/cvrf/v3.0/updates` (also `.../updates('CVE-2020-1048')` to map CVE→document ID).
  - Monthly document: `https://api.msrc.microsoft.com/cvrf/v3.0/cvrf/{YYYY-MMM}` e.g. `.../cvrf/2026-Apr`. One document per Patch-Tuesday cycle; revised in place during the month (track `DocumentTracking.RevisionHistory`).
- **CSAF (added Nov 2024, complementary not a replacement):** provider metadata at `https://msrc.microsoft.com/csaf/provider-metadata.json` (per Microsoft's blog "Toward greater transparency: publishing machine readable CSAF files"). CVRF remains the practical workhorse; CSAF per-CVE files are useful for standardized ingestion alongside Cisco/RedHat/SUSE CSAF.

**CVRF document structure (field names verified from the official `MsrcSecurityUpdates` PowerShell module, `Get-MsrcCvrfAffectedSoftware.ps1`):**

- `ProductTree.FullProductName[]` — `{ProductID, Value}` e.g. ProductID `11923` = "Windows 10 Version 22H2 for x64-based Systems". Product granularity is per-(OS version × servicing channel × arch).
- `Vulnerability[]` — one per CVE, with:
  - `Threats[]` where `Type == 3` → `Description.Value` = **MaximumSeverity** ("Critical"/"Important"…), `Type == 0` → **Impact** (RCE/EoP/…). Threat entries carry `ProductID` lists so severity is per-product.
  - `CVSSScoreSets[]` per product.
  - `Remediations[]` where `Type == 2` (VendorFix / KB), `Type == 5` (Known Issue). Per remediation entry:
    - `Description.Value` → **KB number** (e.g. `5034122`), `URL` → catalog link
    - `ProductID[]` → which products this KB fixes
    - **`FixedBuild`** → e.g. `10.0.19045.3930` — the build (incl. UBR) that contains the fix
    - **`Supercedence`** (Microsoft's spelling) → the KB(s) this update replaces
    - `RestartRequired.Value`, `SubType` (e.g. "Security Update", "Monthly Rollup", "Security Only")

### 2.2 Missing-patch algorithm for Windows OS (the practical, build-number approach)

Cumulative updates make the classic per-KB checklist obsolete for Windows 10/11/Server 2016+: **one monthly cumulative KB per (product, month) fixes all OS CVEs to date, and `FixedBuild` monotonically increases**. The robust algorithm (used in essence by Nessus, Qualys, Wazuh):

1. Inventory: collect `os_product` (map from `os_name`+`DisplayVersion`+`InstalledUBR` registry, or WMI `Win32_OperatingSystem.BuildNumber` + `UBR` from `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion`) → full build string `10.0.<build>.<ubr>`; plus the installed hotfix list (`Win32_QuickFixEngineering`).
2. Map the host to an MSRC `ProductID` (build → product table: 19045→Win10 22H2, 22631→Win11 23H2, 26100→Win11 24H2/Server 2025, 20348→Server 2022, 14393→Server 2016 …; plus arch).
3. For each CVE affecting that ProductID: vulnerable ⇔ `host_build < min(FixedBuild for that ProductID)` **(numeric 4-part compare)**. This sidesteps KB supersedence entirely for the OS.
4. Missing-patch recommendation = the *newest* cumulative KB for that product (all intermediate KBs are superseded); report the count of CVEs it closes.
5. Keep the KB/hotfix check as a fallback for products without `FixedBuild` (older CVRF entries, non-OS products) — vulnerable ⇔ neither the remediating KB nor any KB that (transitively) supersedes it is installed.

**Supersedence graph:** build from the CVRF `Supercedence` field, but treat it as incomplete. Wazuh's experience is a documented cautionary tale: their Windows feed (`https://feed.wazuh.com/vulnerability-detector/windows/msu-updates.json.gz`) contains `MSU` and `MSU_SUPERSEDENCE` tables scraped from MSRC + the Microsoft Update Catalog, and open issues (wazuh/wazuh#14134, #6525, #23541) document false positives caused by KBs missing from the Catalog and holes in supersedence chains — "the only way to check if they have supersedences is to look in the most recent hotfixes to see if they appear in the superseded list." Conclusion: **prefer build-number comparison wherever `FixedBuild` exists; use the supersedence graph only for the KB fallback path, and mark those findings lower-confidence.**

Scanner-side false positives also occur in the other direction: Tenable/Nessus plugins have shipped signatures expecting a *higher* build than Microsoft actually published — a reason to always store the evidence (expected FixedBuild vs observed build) with each finding so operators can adjudicate.

### 2.3 Offline alternative: `wsusscn2.cab` + WUA

Microsoft publishes a signed offline-scan cab (`wsusscn2.cab`, monthly) that the Windows Update Agent can evaluate locally (`AddScanPackageService` → search "IsInstalled=0"), returning exactly the applicable missing security updates with supersedence resolved by WUA itself. This is what MBSA and several commercial patch tools use. Pros: Microsoft's own applicability logic (most accurate possible). Cons: agent-side only, Windows-only, ~1GB file distribution, monthly cadence, and known gaps — the cab does not always carry full supersedence metadata for very old CUs, so WUA can report an older CU missing instead of the newest. For ZenPlus (which has a Windows agent channel via `agent_packages`) this is a credible **phase-2 verification mode**, not the primary engine; the primary engine should be server-side CVRF evaluation so agentless (WinRM/WMI) hosts are covered too.

### 2.4 Windows third-party applications

Inventory reality: Windows apps come from the registry ARP ("Add/Remove Programs") uninstall keys (`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*` + WOW6432Node + per-user HKU) with `DisplayName`, `DisplayVersion`, `Publisher`. These strings are vendor-controlled free text ("Notepad++ (64-bit x64)", "Java 8 Update 381", version "8.0.3810.9").

**How products actually match these (vendor dictionaries, not CPE guessing):**

- **winget** correlates installed ARP entries to package manifests using `AppsAndFeaturesEntries` (`DisplayName`, `Publisher`, `DisplayVersion`, `ProductCode`, `UpgradeCode`) declared in each manifest in `github.com/microsoft/winget-pkgs`, with fuzzy heuristics (strip trailing parenthesised qualifiers like "(64-bit)", match publisher+name; ProductCode exact match wins). The manifest repo is effectively a **free, community-maintained alias dictionary**: ARP strings → canonical package id (`Notepad++.Notepad++`) → latest version + installer URL. A REST reference implementation exists (`microsoft/winget-cli-restsource`); the raw manifests can be mirrored from GitHub.
- **Chocolatey** community repo exposes NuGet v2 OData at `https://community.chocolatey.org/api/v2/` (queryable by package id, `IsLatestVersion`; 10,000-item result cap) — a second source of "latest available version" for ~10k Windows apps.
- **Wazuh** maintains a **CPE helper dictionary** that translates Syscollector-collected Windows program names into CPEs before NVD matching; entries pair source patterns (product name/vendor as it appears in the registry) with the target CPE and version-extraction actions. Its 4.8+ architecture moved this server-side: the Vulnerability Detector Provider (VDP) normalizes all feeds to CVE JSON v5 and merges "internally maintained intelligence data … product name mappings, product translations, and OS base rules." I.e. the alias dictionary is *content*, shipped and updated centrally — exactly the model ZenPlus should adopt (dictionary maintained on zentryc.com, synced like a feed).
- **ManageEngine Vulnerability Manager Plus / SolarWinds Patch Manager** work the same way at larger scale: a central curated database correlates (app, detected version) → vulnerabilities → tested patch, covering "1,100+ third-party apps"; detection logic per app (registry key, file version of the main EXE) is authored per-product by the vendor's content team. The takeaway: for third-party Windows apps, **per-product detection+alias content beats generic CPE inference**; generic CPE matching is the fallback tier only.

Patch mapping for third-party apps = "latest version in winget/Chocolatey/vendor feed ≥ all fixed versions", since third-party installers are almost always full replacements rather than patches.

---

## 3. Linux: distro advisories are ground truth

### 3.1 Why raw NVD version-range matching is WRONG for distro packages

Distros **backport** security fixes: Ubuntu's `openssl 3.0.2-0ubuntu1.15` may contain the fix that upstream shipped in 3.0.11, while NVD's CPE range says "vulnerable < 3.0.11". Consequences:

- Matching distro package versions against NVD/upstream ranges produces massive false positives (patched-but-version-unchanged) and some false negatives (distro-specific regressions, or distro marks a CVE not-affected because the vulnerable code isn't compiled in).
- Fix versions differ per distro release: Red Hat fixed a `requests` CVE in `2.20.0-3` (backport) where upstream required 2.31.0 (Trivy's documented example).
- Severity differs too: Trivy deliberately prefers vendor severity over NVD because "Red Hat evaluates the severity more accurately" for its build options.

**Rule: if a package was installed by the distro package manager (dpkg/rpm/apk), only the distro's own advisory data may decide vulnerable/fixed. NVD is used only for packages the distro does not track (vendor .debs/.rpms, tarball installs) — and flagged lower-confidence.** This is precisely Trivy's documented principle ("For packages installed from OS package managers, Trivy uses the advisory database from the appropriate OS vendor") and Grype's (distro matchers take precedence; CPE matching is a separate, lower-trust matcher).

### 3.2 Per-distro feeds (formats + exact URLs)

| Distro | Primary feed | Format | Notes |
|---|---|---|---|
| **Ubuntu** | `https://security-metadata.canonical.com/osv/` (tarball) or `github.com/canonical/ubuntu-security-notices` (`osv/usn/USN-*.json`, `osv/cve/UBUNTU-CVE-*.json`) | **OSV** | Ecosystem keys `Ubuntu:22.04:LTS`, `Ubuntu:24.04:LTS`, `Ubuntu:Pro:18.04:LTS`; ranges `type: ECOSYSTEM`, events `{introduced: "0"}/{fixed: "2.4.7-1ubuntu2.2"}`; `ecosystem_specific.binaries[]` gives **binary package names+versions** (solves the source-vs-binary problem). OVAL alternative: `https://security-metadata.canonical.com/oval/com.ubuntu.<codename>.usn.oval.xml.bz2`. |
| **Debian** | `https://security-tracker.debian.org/tracker/data/json` | Custom JSON | Shape: `{source_pkg: {CVE-id: {releases: {bookworm: {status: "resolved"/"open", fixed_version, urgency, repositories}}}}}`. Keyed by **source** package; no DSA linkage in the JSON; single ~100MB dump, refreshed continuously. |
| **RHEL** | CSAF-VEX: `https://security.access.redhat.com/data/csaf/v2/vex/` (per-CVE, plus `archive_latest.txt` full archive + daily deltas); advisories at `https://access.redhat.com/security/data/csaf/v2/advisories/` | **CSAF/VEX** | OVAL v1 gone; OVAL v2 streams still updated for RHEL 7/8/9 but Red Hat says migrate to CSAF. VEX states per product/component: fixed / known_affected / known_not_affected / under_investigation — the not-affected signal suppresses FPs. Fix versions are full NEVRA. |
| **Alpine** | `https://secdb.alpinelinux.org/v<branch>/main.json`, `community.json` | secdb JSON | Per package: `secfixes: {"2.8.4-r1": ["CVE-2024-45337", ...]}` — maps *fixed version → CVE list*. Fast and unambiguous; no severity (join NVD for scores). |
| **SUSE/openSUSE** | CSAF 2.0 advisories + VEX from SUSE's FTP/mirror (`https://ftp.suse.com/pub/projects/security/csaf/`) | **CSAF** | Generated since Feb 2023 incl. backfill of all past advisories. |
| **Amazon Linux** | ALAS (`https://alas.aws.amazon.com/AL2023/alas.rss` + repomd updateinfo.xml) | updateinfo | Same model as RHEL: advisory lists fixed NEVRAs. |
| **Others** | AlmaLinux/Rocky errata (OSV on osv.dev), Oracle ELSA OVAL, Fedora updates | mixed | Wazuh's CTI ingests AlmaLinux, Amazon, Ubuntu, Debian, Fedora, Oracle, Red Hat, Rocky, SUSE. |

**OSV as a unifying layer:** the OSV schema (`ossf.github.io/osv-schema`) expresses affected ranges as `ranges[].events[]` (`introduced`/`fixed`/`last_affected`) under an ecosystem whose version-comparison semantics are defined per ecosystem (`ECOSYSTEM` type = use the native comparator, e.g. dpkg compare for Ubuntu). osv.dev also serves an API (`POST https://api.osv.dev/v1/query` for one package@version; `POST /v1/querybatch`, up to 1000 queries, IDs only) and now answers range queries for many Linux distros without version enumeration. For the zentryc.com central service, mirroring Ubuntu/Alma/Rocky as OSV + Debian JSON + RHEL VEX + Alpine secdb, then normalizing all of them into one internal "advisory" schema (as Wazuh's VDP normalizes to CVE JSON v5), is the proven design.

### 3.3 How the reference scanners implement it

- **Grype** (Anchore): `vunnel` pulls ~10 providers (Alpine, Amazon, Debian, GitHub, NVD, Oracle, RHEL, SLES, Ubuntu, Wolfi) → `grype-db` compiles a SQLite `vulnerability.db`, rebuilt daily (tables `vulnerability` = CVE↔package-version-range per namespace, `vulnerability_metadata` = severity/CVSS per source). Matching selects a **namespace** from the detected distro (e.g. `ubuntu:22.04`) so only that distro's records apply; each match records `matchDetails[].type` ∈ `exact-direct-match` (package itself listed), `exact-indirect-match` (matched via source package), `cpe-match` (fuzzy, "requires verification") plus `searchedBy`/`found` evidence and a `Confidence` ratio — a ready-made model for ZenPlus's confidence tiers.
- **Trivy**: per-OS sources (Ubuntu → Ubuntu CVE Tracker+OVAL; Debian → Security Bug Tracker+OVAL; RHEL → OVAL/Security Data; Alpine → secdb; SUSE → CVRF; Amazon → ALAS), GitHub Advisory DB for language ecosystems; severity precedence vendor > CVSS > NVD; `--ignore-unfixed` to hide will-not-fix/not-yet-fixed.
- **Wazuh 4.8+**: agent Syscollector inventories packages (+ Windows hotfixes) into local SQLite; server-side Vulnerability Detection module evaluates against CTI content; VDP pipeline = *content migration* (normalize to CVE JSON v5) → *sanitization* (fix version mismatches/typos, fill gaps) → *merge* with maintained mappings ("product name mappings, product translations, and OS base rules"); all agents re-evaluated when content updates. This "re-evaluate everything on content change" behavior matters for ZenPlus: findings must be recomputable idempotently from (inventory snapshot × feed snapshot).

### 3.4 Version comparison — must be ecosystem-native

Never compare distro versions as semver. Required comparators:

- **dpkg** (Ubuntu/Debian): `[epoch:]upstream[-debian_revision]`; alternating non-digit/digit chunk comparison; `~` sorts *before* empty ("1.0~rc1" < "1.0"). Missing epoch = 0.
- **rpm** (RHEL/SUSE/Amazon): EVR (epoch, version, release) with `rpmvercmp` segment rules, tilde/caret; label-only compare, arch kept separate. RHEL module streams (`modularity`) must match too or FPs occur (documented Grype issue #2452).
- **apk** (Alpine): numeric components + suffixes (`_alpha`, `_rc`, `_p`) + `-r<n>` package revision.
- **Windows**: 4-part numeric build compare; **MSI DisplayVersion** compare is dotted-numeric with vendor quirks — fall back to string equality when unparseable and downgrade confidence.
- **Cisco IOS/IOS-XE/NX-OS**: non-linear trains (`15.2(4)M7`, `17.09.04a`, `9.3(10)`); comparisons are only meaningful **within a train** — this is why Cisco's own API takes the whole version string and returns `firstFixed` per train instead of exposing ranges (§5).

### 3.5 Binary vs source package names

Distro advisories key on **source** packages (`openssl`), hosts have **binary** packages (`libssl3`, `openssl`). Resolution options: dpkg `Source:` field captured at inventory time (best — extend the agent to send it); Ubuntu OSV `ecosystem_specific.binaries[]`; Debian: map via the `Sources`/`Packages` indexes; rpm: `SOURCERPM` header. Grype calls a match made through the source package `exact-indirect-match` — slightly lower confidence than direct.

---

## 4. Generic application matching: name+version → CPE (the fallback tier)

### 4.1 CPE 2.3 and NVD applicability statements

CPE 2.3 formatted string: `cpe:2.3:a:vendor:product:version:update:edition:language:sw_edition:target_sw:target_hw:other`. NVD CVE records (API 2.0) carry `configurations[].nodes[].cpeMatch[]` with `criteria` (a CPE, often version `*`), `vulnerable: true/false`, and range bounds `versionStartIncluding|versionStartExcluding|versionEndIncluding|versionEndExcluding`, plus `matchCriteriaId` resolvable via the CPE Match API. NVD API 2.0 endpoints:

- `https://services.nvd.nist.gov/rest/json/cves/2.0` — 2,000 results/page; incremental sync via `lastModStartDate`+`lastModEndDate` (both required together; ≤120-day window)
- `https://services.nvd.nist.gov/rest/json/cpes/2.0` (dictionary), `.../cpematch/2.0` (match criteria; 5,000/page)
- Rate limits: 5 req/30s without key, **50 req/30s with a free API key**. SolarWinds NCM's precedent: it mirrors `cve-all.json.zip` + `cpematch.json.zip` nightly to the customer appliance — the same "central mirror, appliance syncs a compiled artifact" shape planned for zentryc.com.

### 4.2 Known failure modes of CPE matching (with numbers)

- **Vendor/product naming chaos**: the VulCPE study (arXiv 2505.13895) measured **>50% vendor-name inconsistencies** across NVD CPE data; their remediation framework only reaches precision 0.766 / coverage 0.926 — i.e. even a good CPE pipeline mislabels ~1 in 4 retrievals.
- **Unbounded CPEs**: NVD ships CPEs with version `*` and no range bounds — matches every version forever (Dependency-Track issue #3268 "Bogus CPE strings without version limits ... create recurring false positives").
- **Granularity mismatch**: one CPE covers a whole project when only one artifact/module is vulnerable (classic Maven/Jenkins-plugin FP source).
- **Configuration logic mishandling**: NVD `configurations` use AND/OR nodes (e.g. app AND running-on OS); scanners that flatten them create FPs (Grype issue #1349).
- **Enrichment lag**: since early 2024 NVD analysis has lagged badly; many CVEs sit without CPEs for weeks/months → **false negatives** for any CPE-only pipeline. Mitigations: CVE JSON 5.x `affected[]` data straight from CNAs (mirror `github.com/CVEProject/cvelistV5`) and CISA's ADP "Vulnrichment" (`github.com/cisagov/vulnrichment`) which adds CPEs/CWE/SSVC to un-analyzed CVEs.
- **Registry display names ≠ CPE names**: "Oracle VM VirtualBox 7.0.20" vs `cpe:2.3:a:oracle:vm_virtualbox`; "Java 8 Update 381" vs version `1.8.0_381`(→ `8.0.381`). Without translation content, direct fuzzy matching of ARP strings is FP/FN-riddled — this is exactly why Wazuh requires its CPE-helper dictionary for Windows apps.

### 4.3 The evidence/confidence model (OWASP Dependency-Check — the canonical design)

Dependency-Check's documented internals are the best-described version of the generic approach:

1. Analyzers collect **evidence** into three buckets — *vendor*, *product*, *version* — each item tagged with a confidence: **low / medium / high / highest**.
2. CPE dictionary entries are indexed in **Lucene**; evidence is queried against the index.
3. "When the CPE is determined it is given a confidence level that is equal to the **lowest** level confidence of evidence used during identification."
4. Documented caveat: "Because of the way dependency-check works both false positives and false negatives may exist"; low evidence counts (0–5 items) usually mean no reliable identification.

Adopt the same shape for ZenPlus: every match stores the evidence used (`searched_by`), the rule that produced it, and a tier computed as min(evidence confidence) — and CPE-derived findings are never auto-promoted above "needs review" unless corroborated by a curated alias entry.

### 4.4 Product-alias dictionaries + purl

The pragmatic industry consensus: keep a **curated alias table** mapping observed (normalized_name, publisher) → canonical identity (CPE vendor/product + optional purl + winget id), maintained centrally and updated like a feed. Sources to seed it: winget manifests' `AppsAndFeaturesEntries` (tens of thousands of ARP-string→package mappings, free), Wazuh's public CPE-helper content, and organically from ZenPlus deployments (unmatched inventory rows become dictionary backlog on zentryc.com). Normalization before lookup: lowercase; strip architecture/locale suffixes (`(64-bit)`, `(x64)`, `en-US`), trailing version tokens in the name, publisher suffixes (`Inc.`, `GmbH`); collapse whitespace.

### 4.5 Confidence scoring on matches — concrete tiering

Borrowing Grype's `matchDetails.type` + Dependency-Check's min-evidence rule, a workable 5-tier model:

| Tier | Meaning | Producers |
|---|---|---|
| `vendor_exact` (A) | Vendor's own advisory names this exact product+version (distro feed, MSRC ProductID+FixedBuild, Cisco firstFixed) | distro matcher, MSRC matcher, PSIRT matcher |
| `alias_exact` (B) | Curated alias dictionary mapped the product; version inside advisory/NVD range with bounded range | third-party app matcher |
| `heuristic_high` (C) | Fuzzy CPE match with vendor+product+version evidence all ≥ high, bounded ranges | CPE matcher |
| `heuristic_low` (D) | Fuzzy CPE match with weak/partial evidence, or unbounded CPE, or version compare fell back to string equality | CPE matcher |
| `informational` (E) | Product matched, version unknown/unparseable; or vendor states under_investigation | any |

UI/alerting default: A+B alert; C shown as findings; D/E behind a "low confidence" filter. Every finding carries `match_type`, `evidence` (JSON), `source_feed`, and both observed and fixed versions so operators (and support) can adjudicate — plus per-finding suppress/accept ("false positive" workflow), which every mature product ships because no pipeline reaches zero FP.

---

## 5. Network-device firmware: vendor PSIRT/CSAF feeds, not NVD

### 5.1 Why NVD CPEs are unreliable for firmware

Firmware CPEs in NVD are erratic (`cpe:2.3:o:cisco:ios_xe:17.3.1` vs `...:ios_xe:16.12.1s` vs plain `ios`), version strings are not orderable across trains, and NVD ranges routinely mis-scope platform applicability (a CVE may only affect ASR1k, not Catalyst, at the same IOS-XE version). SolarWinds NCM — which matches NVD `cve-all.json` + `cpematch.json` against node OS versions — is the cautionary precedent: its own forums are full of FP complaints and SolarWinds "is looking at various possibilities to take more data into account in order to eliminate false positives." **Vendor PSIRT feeds encode applicability the way the vendor actually reasons about it (per release train, per platform) and must be preferred; NVD is fallback-tier (D) for vendors without a feed.**

### 5.2 Cisco — openVuln API (the gold standard)

- Auth: OAuth2 client-credentials against `https://cloudsso.cisco.com/as/token.oauth2` (tokens valid 1 hour); free registration on Cisco API Console.
- Rate limits: **5 calls/s, 30 calls/min, 5,000 calls/day**.
- Base: `https://apix.cisco.com/security/advisories/v2/`. Key endpoints:
  - `GET .../OSType/{iosxe|ios|nxos|aci|asa|ftd|fmc|fxos}?version=17.2.1` → all advisories affecting that exact version, each with **`firstFixed`** (list of fixed releases per train, e.g. `["17.3.1w","17.3.2a","17.3.6"]`)
  - `GET .../OS_version/OS_data?OSType=ios` → enumerates valid version strings; `GET .../platforms?OSType=nxos` → platform aliases (`platformAlias` param scopes advisories per platform — important for NX-OS/ASA where applicability is platform-dependent)
  - Plus: by CVE (`.../cve/{id}`), by advisory, `all`, `latest/{n}`, by severity, by date range. Formats: JSON; links to CVRF (being phased out) and CSAF per advisory.
- Coverage windows: ASA/FMC/FTD/FXOS advisories from Jan 2022 onward; NX-OS from Jul 2019 onward (older advisories not queryable by version).
- This API is the machine version of the "Cisco IOS Software Checker". Matching algorithm = *exact version-string lookup*, not range math: normalize the version parsed from `sysDescr` (ZenPlus already extracts it — §1.2) to Cisco's canonical form and query; cache per (OSType, version) since fleets cluster on few versions and the daily quota is 5,000.

### 5.3 Other network vendors

- **Palo Alto**: Security Advisory API (beta) at `https://security.paloaltonetworks.com/api` — `GET /api/v1/products`, `GET /api/v1/products/PAN-OS/{version}/advisories` (e.g. `9.1.3`), filters `severity`, `sort=-cvss`; full advisory JSON at `https://security.paloaltonetworks.com/json/CVE-2024-2551` (CSAF document with product tree and per-version affected/fixed status).
- **Fortinet**: FortiGuard PSIRT advisories (`https://fortiguard.com/psirt`, IDs like `FG-IR-26-141`) with per-advisory CVRF download; affected ranges expressed as version intervals per product (FortiOS 7.4.0–7.4.3 → fix 7.4.4). No public bulk API — scrape/mirror centrally on zentryc.com.
- **Juniper**: JSAs on the Juniper support portal, CVRF/CSAF available; JunOS versioning (e.g. `21.4R3-S5`) needs its own comparator.
- **CSAF as the umbrella**: Cisco, Palo Alto, SUSE, Red Hat, Microsoft all publish CSAF 2.0. A single CSAF ingester (product_tree + `product_status: {fixed, known_affected, known_not_affected, first_fixed}` + `remediations`) covers many vendors; a community-maintained list of vendor CSAF endpoints exists at `github.com/Cyberwatch/CSAF`.

### 5.4 Firmware matching algorithm

1. Identity: (vendor, os_family) from `sys_object_id`/profile (already in ZenPlus), version from `sysDescr`/template OID (e.g. FortiGate OID above), model/PID from entity MIB for platform scoping + EoX.
2. Per vendor adapter: Cisco → openVuln by version string; PAN-OS → advisory API by version; Fortinet/Juniper → evaluate mirrored CSAF/CVRF version ranges with a vendor-specific comparator.
3. Applicability refinement: apply platform filters (`platformAlias`, CSAF product-tree branches) before flagging; a version-only match without platform confirmation drops one confidence tier.
4. Configuration-dependent vulns (e.g. "only if SSH server enabled") cannot be resolved from version alone — flag as `condition: config-dependent` (Cisco advisories state the condition in prose; keep it in the finding). This is what the IOS Software Checker does too.

---

## 6. Patch mapping, recommended versions, EOL

### 6.1 Fixed-version extraction per source

- Linux distro feeds: explicit per-release `fixed_version` (Debian JSON), OSV `events[].fixed`, RHEL VEX fixed NEVRA, Alpine secfixes key. **The remediation is always "upgrade package X to ≥ fixed_version via the distro's own update channel"** — never an upstream version.
- MSRC: remediation = KB id + `FixedBuild`; recommend the newest cumulative KB per product.
- Cisco: `firstFixed` per train; plus advisory-level "recommended release" prose.
- Third-party apps: fixed version from NVD range end / vendor advisory; actionable form = latest winget/Chocolatey/vendor version.

### 6.2 "Recommended version" computation for network gear (SolarWinds/ManageEngine approach)

The operator question is not "which CVEs" but "which single image should I run". Algorithm:

1. Collect open advisories for (device OS, current version) → set of `firstFixed` lists.
2. Constrain to the device's **current train** (e.g. 17.9.x) unless the operator opts into train upgrades; pick `recommended = max(min fixed per advisory)` within the train — i.e. the lowest version in-train that clears *all* open advisories; if some advisory has no fix in-train, escalate to the nearest train that fixes everything.
3. Cross-check the recommendation is not itself affected by *other* advisories (iterate: query the API for the candidate version until fixed-point).
4. Present: current version → recommended version, CVEs closed, train jump y/n. ManageEngine/SolarWinds additionally gate on "vendor-suggested/starred release" (Cisco's ★ gold-star releases) when available.

### 6.3 EOL interplay

- **`endoflife.date`** API v1 (`https://endoflife.date/api/v1/`, docs `https://endoflife.date/docs/api/v1/`): static JSON per product/cycle — `eol` (date or `false`), `support`, `latest` patch version, `lts`, `extendedSupport`; ~460 products incl. Windows, Windows Server, Ubuntu, RHEL, Debian, PAN-OS, FortiOS, Cisco IOS-XE, plus apps (nginx, PostgreSQL…). Beta, but the de-facto standard; mirror on zentryc.com rather than hitting it from appliances.
- **Cisco EoX API** (needs Cisco API Console credentials, same OAuth as openVuln): `https://apix.cisco.com/supporttools/eox/rest/5/EOXByProductID/{pageIndex}/{productID}` and `.../EOXBySWReleaseString/{pageIndex}/{input1}` (input `os,version`), also `EOXBySerialNumber`, `EOXByDates` — returns EoS/EoL/LDoS milestone dates for hardware PIDs and software releases.
- Interplay rules for the module: (a) a device past **end-of-security-support** gets a standing compliance finding regardless of CVE matches; (b) CVE findings on EOL software are marked **unpatchable — remediation is upgrade/replace**, and "recommended version" computation must refuse to recommend an EOL train; (c) Ubuntu ESM/Pro complicates "EOL": `Ubuntu:Pro:18.04:LTS` OSV ecosystems still receive fixes — EOL status must be conditional on entitlement.

### 6.4 Prioritization feeds (cheap wins)

- **CISA KEV**: `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` — flat CVE list with `dueDate`; join on CVE id; "known exploited" badge + sort key.
- **EPSS**: `https://api.first.org/data/v1/epss?cve=CVE-...` (bulk daily CSV also available) — probability of exploitation within 30 days; combined KEV+EPSS>0.5 is the common "fix first" heuristic.
- CVSS: prefer vendor score, then NVD (Trivy precedence), keep both.

---

## 7. Recommended matching pipeline for ZenPlus

### 7.1 Feed architecture (zentryc.com central → appliance)

Appliances must not hit MSRC/NVD/Cisco/canonical directly (rate limits, credentials, air-gap-ish customer networks — public NTP is already blocked in typical deployments). Mirror + compile centrally, in the same shape as the existing OTA flow (`/opt/zenplus/updater`, release artifacts on zentryc.com):

1. **zentryc.com collector jobs** (per feed, staggered daily): NVD API 2.0 incremental (`lastModStartDate` windows, API key, 50 req/30s) + cvelistV5 git mirror + Vulnrichment; MSRC CVRF v3 monthly docs + revisions; Ubuntu OSV tarball; Debian tracker JSON; RHEL VEX archive+deltas; Alpine secdb; SUSE CSAF; Cisco openVuln (cache per advisory + version tables); Palo Alto JSON; Fortinet CVRF scrape; KEV; EPSS; endoflife.date; alias dictionary content.
2. **Normalize** into one internal advisory schema (OSV-like: `advisory(id, source, cve_ids[], severity{vendor,nvd,cvss_vector}, kev, epss)` + `advisory_affected(advisory_id, ecosystem, product_key, range_events JSONB | exact_versions[] | fixed_build | first_fixed_by_train JSONB, platform_scope, status fixed|affected|not_affected|wontfix)`), then compile to a **versioned SQLite/Postgres-dump artifact per content channel** (like grype-db: rebuilt daily, checksummed, downloadable delta) served over the existing zentryc.com release infrastructure.
3. **Appliance sync**: updater-style job pulls the content artifact (version-gated, checksum-verified, resumable); on content change, re-evaluate all assets (Wazuh model) — evaluation must be a pure function of (inventory, content) so results are reproducible and testable.

### 7.2 Matcher chain (first authoritative match wins; later matchers only add lower-tier findings)

```
inventory row ──► 1. Distro matcher   (os_type=linux, pkg source=dpkg/rpm/apk)
                     key: (distro_release, source_pkg) → native version compare → Tier A
              ──► 2. MSRC matcher     (os_type=windows: ProductID + build/UBR vs FixedBuild;
                     hotfix+supersedence fallback → Tier A / C)
              ──► 3. PSIRT matcher    (devices: vendor adapter, exact-version query / CSAF range
                     + platform scope → Tier A; version-only → B)
              ──► 4. Alias matcher    (third-party apps: normalized name+publisher → alias dict →
                     canonical id → NVD/OSV/GHSA bounded ranges → Tier B)
              ──► 5. CPE fuzzy matcher (everything unmatched: evidence buckets vendor/product/version,
                     Lucene/trigram search over CPE dictionary, min-evidence confidence,
                     reject unbounded CPEs from ≥C → Tier C/D)
              ──► 6. EOL matcher      (os + product cycles vs endoflife.date/EoX → compliance findings)
```

Every finding row stores: `asset_ref (server_id|device_id)`, `package_ref`, `cve_id`, `advisory_id`, `source_feed`, `match_type` (mirroring Grype: `exact-direct` / `exact-indirect` / `alias` / `cpe` / `build-compare` / `kb-supersedence`), `confidence_tier`, `evidence JSONB` (searched_by/found, observed vs fixed version), `fix {fixed_version|kb|first_fixed[]|none|wontfix}`, `status (open|patched|suppressed|accepted_risk|false_positive)`, `kev bool`, `epss numeric`, timestamps. Suppression is a first-class operator action with audit (fits existing `audit_logs`).

### 7.3 Inventory collection deltas required (feeds are useless without these)

1. Agent/agentless Windows: OS `CurrentBuild`+`UBR`+`DisplayVersion`; installed hotfix list; ARP entries incl. `Publisher`, `DisplayVersion`, per-user + WOW6432Node, `ProductCode`.
2. Agent/SSH Linux: `/etc/os-release` fields; per-package `source` package + epoch + arch (`dpkg-query -W -f '${Package} ${Version} ${Architecture} ${source:Package}\n'`; `rpm -qa --qf '%{NAME} %{EPOCH}:%{VERSION}-%{RELEASE} %{ARCH} %{SOURCERPM}\n'`).
3. Devices: normalized (vendor, os_family, version) from existing profile extraction; model/PID via ENTITY-MIB for platform scoping + EoX.
4. Schema: extend `server_software_inventory` (arch, pkg_source, source_package, epoch/release, raw_name) — new migration; primary key likely must widen to (server_id, pkg_source, package_name, arch).

### 7.4 Expected accuracy by tier (set operator expectations)

- Tier A (distro/MSRC-build/PSIRT-exact): FP≈0 modulo feed errors; the industry treats these as ground truth.
- Tier B (alias+bounded range): low FP; FN bounded by dictionary coverage — measure "unmatched inventory %" as a KPI and feed it back to the dictionary backlog.
- Tier C/D (CPE fuzzy): literature says expect meaningful error — >50% vendor-name inconsistency in NVD CPE data; best-in-class research precision 0.77. Ship default-off alerting for D, show C with an explicit "verify" affordance, and never let C/D findings drive auto-remediation.

---

## 8. Sources

- MSRC API: [MSRC-Microsoft-Security-Updates-API repo](https://github.com/microsoft/MSRC-Microsoft-Security-Updates-API), [Get-MsrcCvrfAffectedSoftware.ps1](https://github.com/Microsoft/MSRC-Microsoft-Security-Updates-API/blob/main/src/MsrcSecurityUpdates/Public/Get-MsrcCvrfAffectedSoftware.ps1), [MS CSAF announcement](https://www.microsoft.com/en-us/msrc/blog/2024/11/toward-greater-transparency-publishing-machine-readable-csaf-files), [CVRF v3 sample](https://api.msrc.microsoft.com/cvrf/v3.0/cvrf/2026-Apr)
- WUA offline scan: [Using WUA to Scan for Updates Offline](https://learn.microsoft.com/en-us/windows/win32/wua_sdk/using-wua-to-scan-for-updates-offline), [wsusscn2 supersedence gaps](https://learn.microsoft.com/en-us/answers/questions/5600566/when-using-the-wsusscn2-cab-file-to-check-for-offl)
- Wazuh: [How vulnerability detection works](https://documentation.wazuh.com/current/user-manual/capabilities/vulnerability-detection/how-it-works.html), [CPE helper](https://documentation.wazuh.com/current/user-manual/capabilities/vulnerability-detection/cpe-helper.html), [MSU feed & supersedence issues #14134](https://github.com/wazuh/wazuh/issues/14134), [#6525](https://github.com/wazuh/wazuh/issues/6525), [#23541](https://github.com/wazuh/wazuh/issues/23541), [MSU feed thread](https://groups.google.com/g/wazuh/c/R56XoAU4u7A)
- winget/Chocolatey: [AppsAndFeaturesEntries matching](https://github.com/microsoft/winget-cli/discussions/3033), [installer schema](https://github.com/microsoft/winget-pkgs/blob/master/doc/manifest/schema/1.2.0/installer.md), [winget-pkgs](https://github.com/microsoft/winget-pkgs), [Chocolatey OData API](https://docs.chocolatey.org/en-us/community-repository/api/)
- Linux feeds: [Ubuntu OSV docs](https://documentation.ubuntu.com/security/security-updates/osv/), [canonical/ubuntu-security-notices](https://github.com/canonical/ubuntu-security-notices), [Ubuntu OVAL](https://security-metadata.canonical.com/oval/), [Debian tracker JSON](https://security-tracker.debian.org/), [Red Hat Security Data API](https://docs.redhat.com/en/documentation/red_hat_security_data_api/1.0/html-single/red_hat_security_data_api/index), [RH CSAF/VEX GA](https://www.redhat.com/en/blog/csaf-vex-documents-now-generally-available), [alpine-secdb](https://github.com/alpinelinux/alpine-secdb), [SUSE CSAF](https://www.suse.com/support/security/csaf/)
- Scanners: [Where does Grype data come from](https://dev.to/chainguard/deep-dive-where-does-grype-data-come-from-n9e), [Grype match package](https://pkg.go.dev/github.com/anchore/grype/grype/match), [Interpreting Grype results](https://oss.anchore.com/docs/guides/vulnerability/interpreting-results/), [Trivy vulnerability scanner docs](https://trivy.dev/latest/docs/scanner/vulnerability/), [Grype CPE config FPs #1349](https://github.com/anchore/grype/issues/1349)
- CPE quality: [Dependency-Check internals](https://dependency-check.github.io/DependencyCheck/general/internals.html), [reports/confidence](https://dependency-check.github.io/DependencyCheck/general/thereport.html), [VulCPE (arXiv 2505.13895)](https://arxiv.org/abs/2505.13895), [Dependency-Track #3268](https://github.com/DependencyTrack/dependency-track/issues/3268), [NVD data problems](https://vulert.com/blog/nvd-data-quality-problems/)
- OSV: [OSV schema](https://ossf.github.io/osv-schema/), [OSV API for Linux distros](https://osv.dev/blog/posts/announcing-api-queries-for-more-linux-distros/)
- Cisco: [openVuln API docs](https://developer.cisco.com/docs/psirt/), [by-software queries](https://developer.cisco.com/docs/psirt/obtain-advisory-by-software/), [openVuln rate limits/auth](https://sec.cloudapps.cisco.com/security/center/resources/openvulnapi), [openVulnQuery client](https://github.com/CiscoPSIRT/openVulnQuery), [EoX API](https://developer.cisco.com/docs/support-apis/eox/)
- Other vendors: [Palo Alto advisory API](https://security.paloaltonetworks.com/api), [FortiGuard PSIRT](https://www.fortiguard.com/psirt), [vendor CSAF list](https://github.com/Cyberwatch/CSAF)
- NCM precedent: [NCM firmware vulnerability files](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-adding-firmware-vulnerability-files.htm), [NCM FP thread](https://thwack.solarwinds.com/products/network-configuration-manager-ncm/f/forum/45350/firmware-vulnerability-reporting/36546); [ManageEngine VMP database](https://www.manageengine.com/vulnerability-management/vulnerability-database/)
- Prioritization/EOL: [CISA KEV feed](https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json), [EPSS API](https://api.first.org/data/v1/epss), [endoflife.date API v1](https://endoflife.date/docs/api/v1/)
- NVD API: [api-workflows](https://nvd.nist.gov/developers/api-workflows)
