# 03 — Architecture & Data Model

*Status: Design proposal · 2026-08-18. Pinned names live in `00-INDEX.md`. Evidence: `raw/code-*.md`, `raw/research-*.md`.*

---

## 1. End-to-end architecture

```
┌────────────────────────────── zentryc.com (Django, doc 06) ──────────────────────────────┐
│  UPSTREAM INGESTORS (scheduled, credentials & rate limits live HERE, never on appliances) │
│  cvelistV5 (baseline+hourly deltas) · NVD API 2.0 (2h modified-window) · CISA KEV · EPSS  │
│  Cisco openVuln (per-version cache) · Fortinet CVRF · PAN advisory API · Aruba CSAF       │
│  Juniper/Ubiquiti CNA records · MSRC CVRF v3 · Ubuntu/Debian/RHEL/Alpine trackers         │
│  endoflife.date · sysObjectID & product-alias dictionaries (curated content)              │
│        │ normalize (CVE JSON 5.x canonical) → curate slice → validation gate              │
│        ▼                                                                                  │
│  curated corpus (PG) + monotonic offset change-log                                        │
│        ▼ publisher (daily snapshot / 6h deltas)                                           │
│  /feeds/vuln/v1/network-server/  snapshot_*.tar.zst · delta_*.jsonl.zst                   │
│  manifest.json + manifest.sig (Ed25519, zentryc-feed key)                                 │
└──────────────┬───────────────────────────────────────────────────────────────────────────┘
               │ HTTPS pull only (Bearer api_key + X-Appliance-ID — existing OTA auth)
               │ 6h ± 30min, ETag conditional; air-gap: signed .zvb bundle upload
┌──────────────▼───────────────────────── appliance ───────────────────────────────────────┐
│  compliance_feed_loop ──► verify sig+freshness ──► load JSONL → vuln_definitions,         │
│    (lock …95)              (Ed25519 + feed window)  vuln_affects, eol_definitions,        │
│                                                    dictionaries; advance vuln_feed_state  │
│  INVENTORY (existing)                            compliance_match_loop (lock …96)         │
│  devices ◄─ SNMP poller (30s)                      │ matcher chain (§4):                  │
│  device_entities / device_template_values          │  distro ▸ msrc ▸ psirt ▸ alias ▸ cpe │
│  servers + server_software_inventory ◄─ agents     │  + EOL matcher                       │
│  server_patch_inventory (new, KBs)                 ▼                                      │
│                                          vuln_findings / eol_findings / vuln_remediations │
│                                                    │ scoring (§5) · state machine (§6)    │
│                                                    ▼                                      │
│  alerts (rule-driven compliance_* metrics) · compliance_daily_snapshots (trends)          │
│  FastAPI /api/v1/compliance/* ──► React Compliance section (doc 05) · report sections     │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Design invariants:

1. **Appliances never contact upstream sources.** All keys, entitlements, rate limits, scraping, and format wrangling live on zentryc.com. The appliance speaks one hostname over 443 (already the OTA constraint).
2. **Matching is a pure function of (inventory snapshot × feed snapshot).** Re-running it is idempotent; findings are recomputable and testable. State transitions, not re-imports, drive alerts (Wazuh discipline).
3. **Vendor-advisory-first matching.** NVD CPE is the lowest-confidence fallback, never the primary path (post-2026 NVD policy means most new CVEs have no CPE data at all — see doc 01 §2).
4. **Feed content is data, shipped like data**: signed immutable artifacts, snapshot + offset deltas, degrade-to-snapshot, no code execution, empty tables at install are the designed state.

---

## 2. Feed sync (appliance side)

### 2.1 Protocol

Every 6 h ± 30 min jitter (`compliance_feed_loop`):

1. `GET /api/v1/vulnfeed/manifest?channel=network-server` with `If-None-Match` → 304 short-circuits.
2. Verify: Ed25519 signature over raw manifest bytes against `updater/keys/zentryc-feed.pub`; `built` freshness (≤ 7 days old, ≤ 24 h future); `schema_version` major compatibility (newer major → surface "update appliance" banner, do not load). Code reuse note: `updater/crypto.py::verify_signature`/`load_public_key`/`sha256_file` are reused as-is, but `verify_manifest` is NOT — it hardcodes `release_date` + 30-day age; a small `verify_feed_manifest` wrapper checks `built` with the 7-day/24-h window instead (the OTA contract stays untouched).
3. Plan: `head_offset == applied_offset` → done. Local offset within the delta window → download only needed deltas (hundreds of KB). Behind the window / first run / sha mismatch → snapshot + trailing deltas.
4. Download via `updater/downloader.py::download_package` (sha256 from the **signed** manifest, Range resume).
5. Load transactionally — **always diff-based, never truncate/replace**: definitions/EOL/dictionary records upsert on their natural keys; `vuln_affects` rows for a CVE are replaced per-CVE (DELETE by cve_id + INSERT) inside the load transaction — affects rows are ephemeral, which is why finding evidence is self-contained (§4.1); `op: delete` **tombstones** (`vuln_definitions.withdrawn = TRUE`), it never deletes rows — the matcher then closes affected findings through the normal state machine, preserving history and SLAs. Snapshot application diffs against current content the same way (upsert + withdraw-absent). A snapshot reload with zero content change must produce **zero finding churn** (tested, doc 07 T9). `vuln_feed_state.applied_offset` advances in the same transaction. The last verified snapshot archive is kept under `/opt/zenplus/data/compliance/feeds/` for reload.
6. Enqueue matching for changed CVEs only (set `vuln_definitions.needs_match` or record changed cve_ids in the run); `POST /api/v1/vulnfeed/report` with applied offset + duration + status + **fleet telemetry**: observed product tuples (`[{vendor, os_family, version, count}]`) and top unmatched-software names with counts — this is what lets zentryc pre-compile Cisco per-version rules and grow the alias dictionary. Telemetry is disclosed in the UI and can be disabled (`share_telemetry` in settings, default on); with it off, Cisco matching degrades from Tier A (openVuln per-version) to Tier B (CNA-record ranges) and the dictionary backlog loses this appliance's signal.

Failure behavior: any verification or load failure discards the download and keeps serving last-good data; `vuln_feed_state.last_error` is set; a feed older than 7 days raises the `compliance_feed_stale` alert. The loop runs as a normal FastAPI startup task (it only writes DB rows — no root, unlike the OTA updater); it reads `[server] url` + `[appliance]` credentials via the same path `system_updates.py` already uses.

### 2.2 Air-gapped path

- Dashboard: **Compliance → Settings (`/compliance/settings`) → "Upload feed bundle"** accepts a `.zvb` (tar: `manifest.json`, `manifest.json.sig`, snapshot, aux files). Verification is identical to network sync (same functions), plus `snapshot.offset >= applied_offset` (no rollback).
- The customer obtains the bundle from a **pre-authorized tokenized URL on their zentryc.com subscription page** (Nessus custom-URL pattern) — any connected workstation can fetch it without portal login.
- Sideload works **without appliance registration** (verification needs only the public key, which ships with the install).

### 2.3 Entitlement

The module UI is always visible to permitted roles; network feed sync requires the subscription entitlement `features: ["compliance"]` (server adds the key to the subscription object; it flows through checkin verbatim and is cached — **read it from the cached `subscription.json`, not from the local `subscriptions` DB row, which copies only a whitelist of fields**). Unentitled appliances show the module with an activation call-to-action. For sideload, entitlement is enforced **at bundle acquisition** — the pre-authorized download URL only exists on an active subscription's portal page — and the appliance-side load performs no entitlement check (an unregistered appliance has no subscription data to check against; CV-F2 requires sideload to work there). This is a commercial gate, not a security boundary; signature verification is the security boundary and is never skipped.

---

## 3. Data model (Postgres, appliance)

Two migrations, numbered in **ship order** (inventory hardening ships first, in phase CV-E0): **`migrate-080-compliance-inventory.sql`** (§3.3) and **`migrate-081-compliance.sql`** (§3.1–3.2), plus **`migrate-082-compliance-alert-metrics.sql`** (§7). All DDL guarded (`IF NOT EXISTS`), wrapped `BEGIN/COMMIT`, with GRANTs to `zenplus` (tables **and sequences**, house idiom) in guarded `DO $$` blocks, registered in `migrations.lock`. Feed tables ship **empty**.

### 3.1 Feed cache (`migrate-081-compliance.sql`)

```sql
CREATE TABLE IF NOT EXISTS vuln_definitions (
    cve_id          VARCHAR(24) PRIMARY KEY,           -- 'CVE-2026-12345'
    title           TEXT,
    description     TEXT,
    severity        VARCHAR(10) NOT NULL DEFAULT 'unknown'
                    CHECK (severity IN ('critical','high','medium','low','unknown')),
    cvss3_score     NUMERIC(3,1), cvss3_vector TEXT,
    cvss4_score     NUMERIC(3,1), cvss4_vector TEXT,
    severity_source VARCHAR(20),                       -- 'cna' | 'cisa_adp' | 'nvd' | 'vendor'
    epss_score      NUMERIC(6,5), epss_percentile NUMERIC(6,5),
    kev             BOOLEAN NOT NULL DEFAULT FALSE,
    kev_date_added  DATE, kev_due_date DATE, kev_ransomware BOOLEAN,
    exploit_maturity VARCHAR(12),                      -- 'weaponized' | 'poc' | NULL (feed-derived)
    ssvc_automatable BOOLEAN, ssvc_technical_impact VARCHAR(10),  -- CISA Vulnrichment (BOD 26-04 overlay)
    cwe_ids         JSONB NOT NULL DEFAULT '[]',
    "references"    JSONB NOT NULL DEFAULT '[]',       -- [{url, tag}] top refs incl. vendor advisory
    published_at    TIMESTAMPTZ, modified_at TIMESTAMPTZ,
    source_offset   BIGINT NOT NULL,                   -- feed offset that last touched this row
    needs_match     BOOLEAN NOT NULL DEFAULT TRUE,     -- set on upsert; cleared by matcher
    withdrawn       BOOLEAN NOT NULL DEFAULT FALSE,    -- feed op:delete tombstone; rows are NEVER deleted (history survives)
    raw             JSONB NOT NULL                     -- normalized canonical record
);
CREATE INDEX IF NOT EXISTS idx_vuln_defs_severity ON vuln_definitions (severity);
CREATE INDEX IF NOT EXISTS idx_vuln_defs_kev ON vuln_definitions (kev) WHERE kev;
CREATE INDEX IF NOT EXISTS idx_vuln_defs_needs_match ON vuln_definitions (needs_match) WHERE needs_match;

-- Rows are EPHEMERAL: the feed loader replaces a CVE's rules per-CVE (DELETE by cve_id + INSERT)
-- inside the load transaction. Nothing durable may point here except via ON DELETE SET NULL;
-- finding evidence carries a full rule snapshot so adjudication never depends on these rows.
CREATE TABLE IF NOT EXISTS vuln_affects (
    id            BIGSERIAL PRIMARY KEY,
    cve_id        VARCHAR(24) NOT NULL REFERENCES vuln_definitions ON DELETE CASCADE,
    match_kind    VARCHAR(16) NOT NULL
                  CHECK (match_kind IN ('vendor_os','distro_pkg','msrc_build','msrc_kb','alias_pkg','cpe')),
    status        VARCHAR(20) NOT NULL DEFAULT 'affected'   -- VEX vocabulary; not_affected suppresses lower-tier
                  CHECK (status IN ('affected','not_affected','fixed','wontfix','under_investigation')),
    vendor        VARCHAR(80)  NOT NULL,               -- normalized: 'cisco','fortinet','microsoft','ubuntu'…
    product       VARCHAR(160) NOT NULL,               -- normalized: 'ios_xe','fortios','windows_server_2022','openssl'…
    train         VARCHAR(40),                          -- release train/branch ('17.9','7.4','22.04','bookworm')
    version_scheme VARCHAR(20) NOT NULL DEFAULT 'generic'
                  CHECK (version_scheme IN ('generic','semverish','cisco_ios','junos','pan','dpkg','rpm','apk','win_build')),
    version_start TEXT, version_start_incl BOOLEAN NOT NULL DEFAULT TRUE,
    version_end   TEXT, version_end_incl   BOOLEAN NOT NULL DEFAULT FALSE,
    exact_versions JSONB,                              -- for enumerated applicability (Cisco)
    fixed_in      TEXT,                                -- first-fixed within this train
    kb_id         VARCHAR(16),                         -- msrc_kb rows: remediating KB
    fixed_build   TEXT,                                -- msrc_build rows: '10.0.20348.2340'
    platform_scope JSONB,                              -- platform aliases / product-tree scope
    source_feed   VARCHAR(30) NOT NULL,                -- 'cisco_psirt','fortinet_cvrf','ubuntu_osv','msrc','nvd_cpe'…
    cpe23         TEXT,                                -- original CPE when match_kind='cpe'
    condition_note TEXT                                -- 'config-dependent: SSH server enabled' etc.
);
CREATE INDEX IF NOT EXISTS idx_vuln_affects_vp ON vuln_affects (vendor, product);
CREATE INDEX IF NOT EXISTS idx_vuln_affects_cve ON vuln_affects (cve_id);

CREATE TABLE IF NOT EXISTS eol_definitions (
    id            BIGSERIAL PRIMARY KEY,
    vendor        VARCHAR(80) NOT NULL, product VARCHAR(160) NOT NULL,
    cycle         VARCHAR(60) NOT NULL,                -- '7.4', '2022', '22.04'
    release_date  DATE, eoas_date DATE, eol_date DATE, extended_date DATE,
    latest_release TEXT, lts BOOLEAN NOT NULL DEFAULT FALSE,
    source        VARCHAR(30) NOT NULL,                -- 'endoflife.date','cisco_eox','curated'
    UNIQUE (vendor, product, cycle)
);

-- feed-updatable identity dictionaries (content, not code)
CREATE TABLE IF NOT EXISTS compliance_oid_dictionary (
    sys_object_id_prefix VARCHAR(160) PRIMARY KEY,     -- '.1.3.6.1.4.1.9.1'
    vendor VARCHAR(80) NOT NULL, os_family VARCHAR(60), device_type VARCHAR(40)
);
CREATE TABLE IF NOT EXISTS compliance_product_aliases (
    id BIGSERIAL PRIMARY KEY,
    match_name  TEXT NOT NULL,                         -- normalized observed name ('google chrome')
    match_vendor TEXT,                                 -- normalized publisher, nullable
    canon_vendor VARCHAR(80) NOT NULL, canon_product VARCHAR(160) NOT NULL,
    version_transform VARCHAR(40)                      -- e.g. 'java_1x' ('8.0.3810.9'→'8u381')
);
-- match_vendor is nullable; a plain UNIQUE never dedupes NULLs — use the COALESCE-index idiom
-- so the loader's ON CONFLICT target actually fires for publisher-less aliases
CREATE UNIQUE INDEX IF NOT EXISTS idx_cpa_identity
    ON compliance_product_aliases (match_name, COALESCE(match_vendor, ''));
```

### 3.2 Findings & workflow (`migrate-081-compliance.sql`)

Findings are the durable audit record: they are **never deleted by feed activity** (definitions tombstone instead of cascading), only by the asset-cleanup sweeper (§4.2) and the retention prune.

```sql
CREATE TABLE IF NOT EXISTS vuln_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_kind      VARCHAR(10) NOT NULL CHECK (asset_kind IN ('device','server')),
    asset_id        UUID NOT NULL,                     -- devices.id | servers.id (no FK across kinds; §4.2 cleanup sweeper)
    cve_id          VARCHAR(24) NOT NULL REFERENCES vuln_definitions,   -- NO cascade: definitions are tombstoned, never deleted
    package_name    VARCHAR(255),                      -- server software findings: the matched package
    matched_version TEXT NOT NULL,                     -- what we observed
    affect_id       BIGINT REFERENCES vuln_affects(id) ON DELETE SET NULL,  -- convenience pointer; affects rows are ephemeral
    match_kind      VARCHAR(16) NOT NULL,
    confidence      CHAR(1) NOT NULL CHECK (confidence IN ('A','B','C','D','E')),
    evidence        JSONB NOT NULL DEFAULT '{}',       -- SELF-CONTAINED rule snapshot: {searched_by, found, comparator,
                                                       --  source_feed, rule: {…full vuln_affects row…}} — survives feed reloads
    fix             JSONB NOT NULL DEFAULT '{}',       -- {fixed_in | kb | target_version | none | wontfix}
    state           VARCHAR(12) NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','fixed','resurfaced')),
    triage          VARCHAR(20) NOT NULL DEFAULT 'none'
                    CHECK (triage IN ('none','confirmed','not_applicable','risk_accepted','remediation_planned')),
    triage_expires_at DATE,                            -- risk_accepted exceptions expire; matcher reverts triage → 'none' past expiry

    risk_score      NUMERIC(5,1) NOT NULL DEFAULT 0,   -- 0–100, §5
    score_parts     JSONB NOT NULL DEFAULT '{}',       -- explainability: each component
    due_date        DATE,                              -- SLA policy result (KEV due date wins)
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fixed_at        TIMESTAMPTZ
);
-- expression uniqueness needs an index, not a table constraint
CREATE UNIQUE INDEX IF NOT EXISTS idx_vf_identity
    ON vuln_findings (asset_kind, asset_id, cve_id, COALESCE(package_name, ''));
-- "active" uniformly means state <> 'fixed' (open OR resurfaced) — every SLA/score/list query uses this predicate
CREATE INDEX IF NOT EXISTS idx_vf_asset ON vuln_findings (asset_kind, asset_id) WHERE state <> 'fixed';
CREATE INDEX IF NOT EXISTS idx_vf_cve   ON vuln_findings (cve_id);   -- full index: history views + per-CVE joins need fixed rows too
CREATE INDEX IF NOT EXISTS idx_vf_due   ON vuln_findings (due_date) WHERE state <> 'fixed' AND triage NOT IN ('not_applicable','risk_accepted');

CREATE TABLE IF NOT EXISTS vuln_finding_events (
    id          BIGSERIAL PRIMARY KEY,
    finding_id  UUID NOT NULL REFERENCES vuln_findings(id) ON DELETE CASCADE,
    event_type  VARCHAR(24) NOT NULL,                  -- 'detected','state_change','triage_change','comment','score_change'
    from_value  TEXT, to_value TEXT,
    comment     TEXT,
    actor_id    UUID, actor_username VARCHAR(120),     -- NULL for system events
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_vfe_finding ON vuln_finding_events (finding_id, created_at DESC);

CREATE TABLE IF NOT EXISTS eol_findings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_kind  VARCHAR(10) NOT NULL CHECK (asset_kind IN ('device','server')),
    asset_id    UUID NOT NULL,
    eol_id      BIGINT NOT NULL REFERENCES eol_definitions(id),  -- no cascade; EOL rows are upserted on (vendor,product,cycle), never delete/reinserted
    milestone   VARCHAR(12) NOT NULL CHECK (milestone IN ('approaching','eoas','eol','extended')),
    state       VARCHAR(12) NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved')),
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_kind, asset_id, eol_id, milestone)  -- an asset can be past EOAS and approaching EOL at once
);
CREATE INDEX IF NOT EXISTS idx_eolf_eol ON eol_findings (eol_id);

CREATE TABLE IF NOT EXISTS vuln_remediations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_kind    VARCHAR(10) NOT NULL, asset_id UUID NOT NULL,
    action_type   VARCHAR(20) NOT NULL
                  CHECK (action_type IN ('upgrade_os','install_kb','update_package','replace_eol')),
    target        TEXT NOT NULL,                       -- '17.9.5' | 'KB5034122' | 'chrome 152.x'
    train_jump    BOOLEAN NOT NULL DEFAULT FALSE,
    clears_cve_ids JSONB NOT NULL DEFAULT '[]',
    clears_count  INTEGER NOT NULL DEFAULT 0,
    risk_cleared  NUMERIC(8,1) NOT NULL DEFAULT 0,     -- sum of risk_score cleared
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_kind, asset_id, action_type, target)
);

CREATE TABLE IF NOT EXISTS compliance_asset_meta (
    asset_kind  VARCHAR(10) NOT NULL, asset_id UUID NOT NULL,
    criticality SMALLINT NOT NULL DEFAULT 3 CHECK (criticality BETWEEN 1 AND 5),
    internet_facing BOOLEAN NOT NULL DEFAULT FALSE,
    excluded    BOOLEAN NOT NULL DEFAULT FALSE,        -- opt this asset out of matching
    updated_by  UUID, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_kind, asset_id)
);

CREATE TABLE IF NOT EXISTS compliance_daily_snapshots (
    day             DATE PRIMARY KEY,
    open_by_severity JSONB NOT NULL,                   -- {critical: n, high: n, …}
    kev_open        INTEGER NOT NULL, overdue INTEGER NOT NULL,
    eol_assets      INTEGER NOT NULL,
    avg_asset_risk  NUMERIC(6,1), total_assets INTEGER NOT NULL,
    fixed_that_day  INTEGER NOT NULL DEFAULT 0, new_that_day INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vuln_feed_state (
    channel         VARCHAR(40) PRIMARY KEY,
    schema_version  VARCHAR(16),
    applied_offset  BIGINT NOT NULL DEFAULT 0,
    snapshot_offset BIGINT,
    last_sync_at    TIMESTAMPTZ, last_success_at TIMESTAMPTZ,
    last_manifest_etag TEXT, last_error TEXT,
    definitions_count INTEGER NOT NULL DEFAULT 0
);

-- Roles backfill (new permissions never reach existing DB role rows otherwise — verified gap).
-- UPDATEs are invisible to the replay-safety analyzer, so each must be self-guarded/idempotent:
UPDATE roles SET permissions = permissions || '["compliance.view"]'::jsonb
 WHERE is_system AND name IN ('operator','viewer','read_only') AND NOT permissions ? 'compliance.view';
UPDATE roles SET permissions = permissions || '["compliance.triage","compliance.manage"]'::jsonb
 WHERE is_system AND name = 'operator' AND NOT permissions ? 'compliance.manage';
```

Caveat (accepted): if the migration runner ever *baselines* this file (objects present, ledger row missing — only possible after a manual partial apply), the backfill UPDATEs are skipped; admin works regardless via `system.admin`, and a role edit repairs the rest. The catalog in `core/permissions.py` and `LEGACY_ROLE_PERMISSIONS` are updated in the same release.

### 3.3 Inventory hardening (`migrate-080-compliance-inventory.sql` — ships first, phase CV-E0)

```sql
ALTER TABLE devices ADD COLUMN IF NOT EXISTS os_family VARCHAR(60);        -- 'ios_xe','fortios','panos','routeros'…
ALTER TABLE devices ADD COLUMN IF NOT EXISTS sys_descr TEXT;               -- persisted for re-extraction
ALTER TABLE devices ADD COLUMN IF NOT EXISTS identity_updated_at TIMESTAMPTZ;  -- stamped by the poller on every successful system-info poll

-- Normalized distro identity (distro feeds key on exact release, not free-text os_name)
ALTER TABLE servers ADD COLUMN IF NOT EXISTS distro_id VARCHAR(40);        -- /etc/os-release ID: 'ubuntu','debian','rhel'
ALTER TABLE servers ADD COLUMN IF NOT EXISTS distro_release VARCHAR(40);   -- VERSION_ID: '22.04','12'
ALTER TABLE servers ADD COLUMN IF NOT EXISTS distro_codename VARCHAR(40);  -- VERSION_CODENAME: 'jammy','bookworm'

ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS arch VARCHAR(16) NOT NULL DEFAULT '';
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS pkg_source VARCHAR(20) NOT NULL DEFAULT 'registry';  -- 'registry','msi','dpkg','rpm','apk'
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS source_package VARCHAR(255);
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS raw_name VARCHAR(255);
ALTER TABLE server_software_inventory ADD COLUMN IF NOT EXISTS product_code VARCHAR(64);   -- MSI ProductCode (winget-exact matching)
-- PK widens to (server_id, pkg_source, package_name, arch): dpkg multiarch (libssl3:amd64 vs :i386)
-- and same-named packages from different sources must not collapse last-write-wins.
-- Guarded constraint swap; existing rows already satisfy the defaults above:
ALTER TABLE server_software_inventory DROP CONSTRAINT IF EXISTS server_software_inventory_pkey;
ALTER TABLE server_software_inventory ADD PRIMARY KEY (server_id, pkg_source, package_name, arch);

CREATE TABLE IF NOT EXISTS server_patch_inventory (
    server_id    UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    patch_id     VARCHAR(20) NOT NULL,                 -- 'KB5034122'
    title        VARCHAR(255), installed_on TIMESTAMPTZ,
    source       VARCHAR(20) NOT NULL DEFAULT 'agent',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (server_id, patch_id)
);
```

(The PK swap is not probeable as "added column" evidence on its own, but the same file adds columns and a table, so the migration classifies; the DROP/ADD pair is the recognized replay-safe idiom.)

Poller/collector changes paired with 080 (Go + Python, no schema): persist sysDescr, walk `entPhysicalSoftwareRev`, **stamp `identity_updated_at` on every successful system-info poll**; promotion job copies chassis `model_name`/`serial_number` from `device_entities` into `devices` (precedence: template identity value ▸ chassis entity ▸ profile extraction ▸ discovery ▸ manual; never overwrite operator-set values); template packs gain identity key mappings so `pan_sw_version`/`f5_version`/`fgt_fw_version` write `devices.os_version` — **this writeback is the only PAN-OS fix: PAN's profile already has an `extract_os_version` rule that can never fire because PA sysDescr carries no version string** (F5/ASA extract-rule additions only where sysDescr actually carries a version — verify first); **vendor packs' `children` mappings gain `os_version_key`/`serial_key` for FortiAP and Aruba AP children** (today they get model only — without this, promoted APs stay permanently un-evaluable); ingest branch for `inventory.patches` (prune-on-snapshot idiom); agent (separate repo) adds `patch_inventory_v1` capability collecting Get-HotFix + CurrentBuild/UBR + ARP `ProductCode`/arch/source fields + `/etc/os-release` identity on Linux.

---

## 4. Matching engine (`compliance_match.py`)

### 4.1 Matcher chain

First authoritative matcher wins per inventory row; later matchers may only add lower-tier findings for rows the earlier ones didn't claim:

```
device  ──► PSIRT matcher    (vendor_os rows: exact-version sets or per-train ranges,
                              platform scope check; version-only match without platform
                              confirmation drops one tier)                       → Tier A/B
        ──► CPE matcher      (devices with no vendor_os coverage)                → Tier C/D
        ──► EOL matcher      ((vendor, product|os_family, cycle) vs eol_definitions)

server  ──► distro matcher   (linux + pkg_source dpkg/rpm/apk: (release, source_pkg)
                              native comparator vs distro-feed ranges)           → Tier A
        ──► MSRC matcher     (windows: ProductID from os mapping; build+UBR vs
                              fixed_build numeric compare — primary; KB presence/
                              supersedence fallback at lower confidence)         → Tier A/C
        ──► alias matcher    (third-party apps: normalize name+publisher →
                              compliance_product_aliases → bounded ranges)       → Tier B
        ──► CPE matcher      (unmatched apps; evidence-scored, unbounded CPEs
                              rejected above D)                                  → Tier C/D
        ──► EOL matcher      (OS + tracked products)
```

Key correctness rules (from `raw/research-package-matching.md`):

- **Distro packages are only judged by distro feeds** (backports make NVD ranges wrong); NVD applies only to non-distro-managed software, flagged lower confidence.
- **Windows OS findings prefer build-number comparison** (`kernel_or_build` ≥ `fixed_build` per product) over KB supersedence graphs (documented holes → false positives); KB fallback findings are Tier C.
- **Cisco is never CPE-matched.** Applicability comes from openVuln-derived exact-version sets/`firstFixed` in the feed. **Both sides normalize to Cisco canonical form before comparison** — IOS-XE zero-padded forms (`17.06.04` ≡ `17.6.4`), train suffixes, and `03.x` platform-style numbering break naive string equality; the same canonicalizer runs in the zentryc compiler and in the appliance matcher, with equivalence cases pinned in the T1 fixture corpus.
- **`not_affected` rules suppress**: a vendor VEX `known_not_affected` rule for (product, version) vetoes any lower-tier (CPE/alias) finding for the same (asset, CVE); `wontfix`/`under_investigation` rules produce Tier-E informational findings with `fix: {wontfix}` / no fix, keeping the "Fix available" filter honest.
- **Version comparators are per `version_scheme`**: `dpkg`, `rpm` (epoch:version-release), `apk`, `win_build` (4-part numeric), `cisco_ios` (train-scoped), `junos`, `pan` (`-hN` hotfix), `semverish`, `generic` (existing `baseline_service` tokenizer). Comparisons across trains are refused — a rule scoped to train `17.9` never evaluates a `16.x` device.
- Every finding stores its **evidence** (rule id, comparator, observed vs fixed) so operators and support can adjudicate; false positives are triaged (`not_applicable`), never silently suppressed.

### 4.2 Scheduling & performance

- `compliance_match_loop`: 5-minute tick, advisory lock 1515074396. Work sources, in order: (a) CVEs with `needs_match` (post-feed-sync incremental), (b) assets whose inventory changed since last evaluation (hook: software-ingest trigger sets a dirty flag, same place baselines re-evaluate — `host_metric_service.py:631–647`; device identity promotion job marks devices dirty), (c) full nightly re-evaluation at the configured hour (default 02:00 appliance-tz) as a safety net.
- Batched writes; per-run bounded (e.g. 200 assets / 2,000 CVE evaluations per tick, continue next tick); never on the request path. At 10k devices × ~50 relevant rules/vendor this is minutes of work nightly — well within the 4 vCPU envelope, and incremental runs are seconds.
- State computation per (asset, cve): match present & previously `fixed` → `resurfaced` (**and it stays `resurfaced` until it stops matching** — it is a persistent active state, not a one-tick event); match present & previously open/resurfaced → unchanged (`last_seen` rolls); match newly present → `open`; match absent & previously active → `fixed` + `fixed_at` (this is **patch verification**: version changed → findings auto-close, and the `vuln_finding_events` trail is the before/after rescan evidence auditors ask for). "Active" always means `state <> 'fixed'`. Triage survives state changes except `not_applicable` findings that re-match after a version *change* reset to `none` (the basis for the dismissal is gone). A CVE tombstoned by the feed (`withdrawn`) closes its findings with a `withdrawn` event.
- **Identity-staleness gate:** devices whose `identity_updated_at` is older than 7 days (configurable) or whose status is down are not re-evaluated — existing findings are kept but flagged `identity_stale` in the UI, and such devices count into "not evaluable", never into "clean". Prevents a dead/replaced device from generating verdicts off last-known firmware forever (the sticky-COALESCE trap).
- **Asset cleanup sweeper** (runs inside `compliance_match_loop`): findings/EOL findings/remediations/meta rows whose `(asset_kind, asset_id)` no longer resolves to a live device/server are deleted (the asset is gone — its ghost must not inflate per-CVE asset counts, KPIs, or fleet ARS). Tested: deleting a device drops its counts (doc 07 T21).

### 4.3 Remediation computation (`vuln_remediations`)

Recomputed per asset after matching:

- **Network device:** collect open findings' `fixed_in` per train → recommended = lowest in-train version clearing all open advisories (fixed-point check: the candidate must itself have no open advisories); if impossible in-train, propose nearest fixing train with `train_jump = true`. Present "current → target, clears N CVEs, risk cleared".
- **Windows server:** newest cumulative KB per product from `msrc_build`/`msrc_kb` rows (supersedence sinks), plus per-app `update_package` targets.
- **Linux server:** per-package `fixed_version` from the distro feed ("upgrade via distro channel").
- **EOL assets:** `replace_eol` action; recommendation engine refuses to recommend an EOL train.

---

## 5. Risk scoring (`compliance_score.py`) — transparent, no ML

Design constraints: recomputable by hand from stored components; monotonic; degrades gracefully when CVSS/EPSS are missing (the post-2026 NVD reality); mirrors the market's two-level shape so buyer mental models transfer. Full derivation and worked examples: `raw/research-frameworks-scoring.md` §2.4.

### 5.1 Finding Risk Score (FRS, 0–100) — risk = impact × threat × exposure × match quality

```
FRS = min(100, 100 × I × T × E × Q)

I (impact)   = CVSS_effective / 10
               CVSS_effective = cvss4 (BT) if present, else cvss3, else severity-mapped:
               critical 9.5 / high 8.0 / medium 5.5 / low 3.0 / unknown 6.0
T (threat)   = highest matching rung:
               1.00  KEV + known ransomware use
               0.90  KEV
               0.80  weaponized public exploit OR EPSS ≥ 0.50
               0.60  PoC public OR EPSS 0.10–0.50
               0.40  EPSS 0.01–0.10
               0.25  EPSS < 0.01 / unscored, no known exploit
E (exposure) = 1.15 internet-facing · 1.00 internal · 0.85 isolated/mgmt-VLAN (admin-tagged)
Q (quality)  = 1.00 tier A/B · 0.85 C · 0.60 D · 0.30 E
```

FRS bands: **Critical ≥ 70 · High 45–69 · Medium 20–44 · Low < 20.** Sanity anchors: CVSS 10.0 KEV on an internet-facing firewall → 100; an unexploited paper-critical (CVSS 9.8, EPSS 0.005) → ~24 (Medium risk) — this is deliberately VPR/TruRisk-like behavior, so the UI **always shows CVSS severity alongside FRS** ("Critical severity · Medium risk") or operators will distrust the number. A CVSS 6.5 with EPSS 0.92 on an exposed asset (~60, High) correctly outranks it. The T-ladder (bands, not raw multiplication) exists because EPSS is heavily right-skewed. All components live in `score_parts` and render as a one-line breakdown.

### 5.2 Asset Risk Score (ARS, 0–1000)

```
ARS = min(1000, K × (100·√n_C + 40·√n_H + 10·√n_M + 2·√n_L))
n_s = active findings (state <> 'fixed') in FRS band s (excluding not_applicable / unexpired risk_accepted)
K   = 0.5 / 0.75 / 1.0 / 1.25 / 1.5 for criticality 1–5
```

Bands align with Qualys so buyer intuition transfers: **Severe ≥ 850 · High 700–849 · Medium 500–699 · Low < 500.** √-dampening keeps a hundred lows below one critical; K keeps a criticality-5 asset with one critical above a criticality-1 asset with several. Criticality defaults auto-suggest by device class (core switch/firewall/server → 4, access switch → 3, other → 3) with per-asset override. Fleet trend = daily snapshots.

### 5.3 SLA policy — data-driven packs

SLA policies are **data** (`(matcher → days_to_due)` rules over {KEV flags, FRS band, exposure}; first match wins, KEV rules outrank band rules), stored in the `'compliance'` settings blob. Default pack ("ZenPlus Default", ISO A.8.8-defensible):

| Rule | Due |
|---|---|
| KEV + ransomware, or KEV on internet-facing asset | 7 days |
| KEV (any asset) | 14 days |
| FRS Critical (≥ 70) | 15 days |
| FRS High | 30 days |
| FRS Medium | 90 days |
| FRS Low | 180 days |
| EOL reached | 90 days to an upgrade plan (own finding type) |

**Patch/SLA compliance % is eligibility-scoped** (auditor norm): the denominator excludes decommissioned assets, assets not evaluated in > 30 days, and excluded assets; exceptions (risk_accepted) are excluded from the numerator but **counted and shown separately**. The companion coverage KPI — % of in-scope assets evaluated in the last 24 h/7 d, target ≥ 95% — ships in `/compliance/summary` and on the Overview (it is the auditor's first question).

Due dates are **dynamic**: KEV addition or exposure change re-derives them (BOD 26-04 behavior). Named policy packs ship later as content: PCI DSS (30 d critical/high, rescan-to-clean), **Federal/BOD 26-04** (the 3/14/60-day/defer matrix — pure lookup, since the feed carries Vulnrichment `ssvc_automatable`/`ssvc_technical_impact` and the asset carries `internet_facing`), Essential Eight, HIPAA-proposed. Note: BOD 22-01 was revoked in 2026; BOD 26-04 is the current federal reference — the KEV catalog and its `dueDate` field remain live inputs.

### 5.4 Exceptions are auditor-facing objects

`risk_accepted` triage requires a comment and supports `triage_expires_at`; expired exceptions auto-revert to `none` and resurface. The exception list (asset, finding, justification, approver, expiry) is exportable — auditors ask for the risk-acceptance log by name, and 12 months of finding history is retained by default.

---

## 6. State machines

```
auto state:    open ──(no longer matches)──► fixed ──(matches again)──► resurfaced ──► fixed …
triage:        none ──► confirmed | not_applicable | risk_accepted | remediation_planned
               (any → any, always with optional comment; every change appends vuln_finding_events;
                risk_accepted requires a comment; bulk transitions supported)
```

Alert discipline (Wazuh rule, all three suppressions): alerts fire only on **transitions** — a new `open` finding at/above the alert threshold, a KEV arrival, feed staleness crossing 7 days, an asset crossing into EOL. Feed re-imports and re-evaluations that produce no state change are silent, **and so is an asset's first-ever evaluation**: findings created the first time an asset (or the whole module) is evaluated raise no channel notifications — the Overview and a one-time digest carry the initial picture instead. Without this, enabling the module on an existing fleet floods every channel on day one and gets the module disabled.

---

## 7. Alerting integration

- New rule-driven metrics via `migrate-082-compliance-alert-metrics.sql` — a constraint supersede whose base is **the current latest owner of `alert_rules_metric_check`, which is migrate-076 (netpath metrics), NOT migrate-073** despite 073's "canonical" name (re-verify the latest owner at implementation time): 076's full list verbatim + the `tpl\_%` escape clause + the `compliance_*` keys; `alert_rules` is superuser-owned (076's header note applies). Metrics: `compliance_critical_open` (count of active findings ≥ configured severity on the asset), `compliance_kev_open`, `compliance_eol_reached` (asset-scoped), `compliance_feed_stale` (appliance-scoped). Evaluated by `compliance_alerts.py` following `udt_alert_service.py` shape, raise/resolve deduped per (asset, metric), dispatched through normal alert rules → notification channels (unlike NCM's channel-less inserts).
- Wording added to `alert_phrasing.py` (it owns all notification copy).
- Threshold defaults: alert on Tier A/B findings with severity ≥ high; everything else is dashboard-only. All thresholds editable in Compliance → Settings (`/compliance/settings`).

---

## 8. API surface (summary — full contracts in doc 04)

```
GET  /api/v1/compliance/summary                      overview KPIs + trend + aging      [view]
GET  /api/v1/compliance/vulnerabilities              CVE list w/ per-CVE asset counts   [view]
GET  /api/v1/compliance/vulnerabilities/{cve_id}     detail + affected assets           [view]
GET  /api/v1/compliance/findings                     finding instances (filters)        [view]
POST /api/v1/compliance/findings/bulk-triage         state change + comment (audited)   [triage]
GET  /api/v1/compliance/assets                       per-asset posture list             [view]
GET  /api/v1/compliance/assets/{kind}/{id}           one asset's findings/EOL/remeds    [view]
PUT  /api/v1/compliance/assets/{kind}/{id}/meta      criticality/exposure/exclude       [manage]
GET  /api/v1/compliance/remediations                 grouped patch recommendations      [view]
GET  /api/v1/compliance/eol                          EOL findings + timeline            [view]
GET  /api/v1/compliance/software                     fleet software × vuln aggregation  [view]
GET  /api/v1/compliance/feed/status                  sync state, offsets, ages          [view]
POST /api/v1/compliance/feed/sync-now                trigger sync (authenticated!)      [manage]
POST /api/v1/compliance/feed/upload-bundle           air-gap .zvb import                [manage]
GET|PUT /api/v1/compliance/settings                  thresholds, SLA, schedule          [manage]
POST /api/v1/compliance/evaluate-now                 force full re-match                [manage]
```

Conventions: `require_permission` bindings at router top; inline Pydantic models; raw `text()` SQL; `write_audit_log` on every mutation; list endpoints paginated + facet-counted like `/servers`.

---

## 9. Scale & safety notes

- Feed slice ≈ 60–95k CVEs → `vuln_definitions` tens of MB in PG; `vuln_affects` a few hundred k rows — trivial for Postgres with the pinned indexes.
- Findings volume: worst case (unpatched fleet) ~tens of findings/asset → 10k assets × 50 = 500k rows; list endpoints always filter by state/severity and paginate.
- All loops: transaction-scoped advisory locks; bounded batches; the shutdown tuple in `main.py:204` must list both new task attrs. **Comparator/scoring batches are CPU-bound Python and run via `asyncio.to_thread`** (small per-slice event-loop occupancy) — the API event loop is a documented scarce resource (agent ingest saturates ~240 agents), and a minutes-long match run must never starve heartbeat/ingest handling (doc 07 T17 asserts agent-ingest latency is unaffected during a full re-match).
- Multi-appliance/HA (doc 24 architecture): feed state and findings are per-appliance; the AA-controller design treats them like any other module tables.
- Security: no credential material in any compliance response; sync-now and upload endpoints are permission-gated (the NCM unauthenticated-endpoint mistake is explicitly not repeated); bundle verification is mandatory even for admin uploads.
