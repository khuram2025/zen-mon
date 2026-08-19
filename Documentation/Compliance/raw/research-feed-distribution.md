# Research: Feed-Distribution Architectures for the ZenPlus Compliance & Vulnerability Module

*Raw research report — feed distribution from central server (zentryc.com, Django) to on-prem appliances.*
*Investigator output, 2026-08-18. All "measured live" numbers were fetched from the public endpoints on this date from this appliance (outbound HTTPS from the appliance works; NVD API, GCS, ghcr.io, cti.wazuh.com, grype.anchore.io, cisa.gov, epss.empiricalsecurity.com and endoflife.date were all reachable).*

---

## 1. What ZenPlus already has (baseline to reuse)

The OTA updater already implements ~80% of the appliance↔zentryc.com sync machinery a vulnerability feed needs: registration with a license key, Bearer-token auth, a resumable verified downloader, and Ed25519 detached-signature manifest verification. The vuln feed should be an additional *content channel* on this exact stack, not a new protocol.

### 1.1 Server config and auth

`/opt/zenplus/updater/config.py`:

- Line 11: `SUBSCRIPTION_PATH = UPDATER_DIR / "config" / "subscription.json"` — subscription/entitlement cache (plan, slots). A vuln-feed entitlement flag belongs in this same document.
- Lines 15–18:
  ```python
  class ServerConfig:
      url: str = "https://zentryc.com"
      check_interval_seconds: int = 900
  ```
- Line 29: `public_key_path: str = str(UPDATER_DIR / "keys" / "zentryc-release.pub")` — pinned Ed25519 release public key shipped with the appliance.

`/opt/zenplus/updater/agent.py`:

- Lines 71–87, `_api_headers()`: sends `Authorization: Bearer {cfg.appliance.api_key}` and `X-Appliance-ID: {cfg.appliance.id}`. Headers are omitted until registered (an empty Bearer header is treated as malformed by the server).
- Line 101, `register()`: `POST /api/v1/appliances/register` with body `{hostname, arch, os_version, current_version, registration_token}` where `registration_token` is the **license key** from the Zentryc subscription page. Response carries `appliance_id`, `api_key`, and a `subscription` object (`name`, `plan`, `used_slots`, `max_appliances`).
- Line 148, `checkin()`: `POST /api/v1/appliances/checkin` with the inventory dict.
- Line 209, `check_for_update()`: `GET /api/v1/updates/check?current_version=&arch=` → `{available, release}` where `release` includes `version`, `package_url`, `package_sha256`, `manifest_sig` (base64).
- Line 231, `report_status()`: `POST /api/v1/updates/report` `{release_id, status, from_version, to_version, error_message, log_data}` — the telemetry/ack pattern to copy for feed-apply results.
- `GET /api/v1/appliances/subscription` (line ~196) refreshes entitlements on demand.

### 1.2 Resumable verified downloader

`/opt/zenplus/updater/downloader.py`:

- Line 13: `CHUNK_SIZE = 65536`.
- Line 20, `download_package(url, dest_path, expected_sha256, ...)`:
  - Pre-check: if the destination file already exists and its sha256 matches, skip the download entirely (idempotent retry).
  - Line 48: resume via `req_headers["Range"] = f"bytes={resume_from}-"`; on HTTP 416 it deletes the partial and restarts (lines 57–64).
  - Post-download sha256 verification against `expected_sha256`.

### 1.3 Signature + freshness verification

`/opt/zenplus/updater/crypto.py`:

- Ed25519 keypair generation/loading via `cryptography` (lines 25–70).
- Line 73, `verify_signature(data, signature, public_key)` — raw Ed25519 detached signature.
- Line 126, `verify_manifest(manifest_path, signature_path, public_key_path, max_age_days=30)` — checks, in order: (1) Ed25519 signature over the raw manifest bytes, (2) `release_date` not older than `max_age_days` (anti-freeze/rollback), (3) `release_date` not more than 24h in the future, (4) JSON well-formed. **This is exactly the freshness+integrity model TUF prescribes for a single-publisher feed, already implemented.**

**Implication:** the feed system needs only (a) new content endpoints on the Django server, (b) a feed-specific signing key (recommended: separate from the release key so the release key stays cold), (c) a loader on the appliance.

---

## 2. Case study: Grype (Anchore) vulnerability DB distribution

Reference OSS design for "central builder → static CDN → many pull clients". Sources: [Anchore grype-db architecture docs](https://oss.anchore.com/docs/architecture/grype-db/), [v5→v6 blog](https://anchore.com/blog/grype-db-schema-evolution-from-v5-to-v6-smaller-faster-better/).

### 2.1 Pipeline (central side)

Three-stage daily pipeline: **pull** (vuln data from upstream providers: Alpine, Amazon, Debian, GitHub, NVD, …; provider outputs are cached as OCI artifacts in a "vulnerability cache" repo at `ghcr.io/anchore/grype`), **build** (transform into every supported SQLite schema version), **package** (compressed archive). Only databases that pass a validation gate ("vulnerability-match-labels" regression comparison against the previous release) are uploaded. Hosting is Cloudflare R2 (S3-compatible) behind `https://grype.anchore.io/databases/`.

### 2.2 Discovery files — two generations

**v5 (legacy), `listing.json`** at `databases/listing.json`: one big file `{ "available": { "1": [...], "2": [...], "5": [...] } }` — URLs to archives keyed by schema version, latest-date-first. Regenerated daily as a separate step (race-prone; superseded).

**v6 (current), `latest.json`** at `databases/v{major}/latest.json`, generated and uploaded **atomically with the archive**, 5-minute CDN cache TTL. Measured live 2026-08-18 (`https://grype.anchore.io/databases/v6/latest.json`, 249 bytes):

```json
{
 "status": "active",
 "schemaVersion": "v6.1.9",
 "built": "2026-08-18T06:15:38Z",
 "path": "vulnerability-db_v6.1.9_2026-08-18T00:15:37Z_1787033738.tar.zst",
 "checksum": "sha256:32d6bf90b2a034bd6ca3aa71d90d8bcdf1ed0e521f98f1f00a970ae2af3318c7"
}
```

Notable fields: `status` (lets the publisher mark a channel `deprecated`/`eol` remotely), semver'd `schemaVersion` (client refuses majors it doesn't understand), sha256 checksum, monotonic epoch suffix in the filename. **This tiny-pointer-file + immutable-artifact pattern is the single most copyable idea for ZenPlus.**

### 2.3 Format, sizes, cadence (measured/published)

- Artifact: SQLite (v6: "blob table" model — small indexed lookup tables pointing into JSON blobs) in a `.tar.zst` (zstd replaced gzip in v6).
- Published sizes: v5 raw 1.6 GB / 210 MB compressed; v6 raw 900 MB / 65 MB compressed at launch.
- **Measured live 2026-08-18: the current v6 archive is 145,102,677 bytes ≈ 145 MB** (`Accept-Ranges: bytes` — resumable), i.e. the DB has grown ~2.2× since the v6 launch blog. Plan for growth.
- Cadence: built and distributed **daily**.
- Delta strategy: **none** — full replacement daily. Clients check `latest.json`, compare `built` to local metadata, download whole archive if newer. Simplicity over bandwidth.
- Signing: checksum only, no signature (transport trust = TLS + CDN). ZenPlus should do better (it already has Ed25519 manifests).

## 3. Case study: Trivy (Aqua) DB distribution — OCI artifacts

Sources: [trivy-db repo](https://github.com/aquasecurity/trivy-db), [Trivy air-gap docs](https://trivy.dev/docs/v0.57/guide/advanced/air-gap/).

- Distribution channel: **OCI artifact on GHCR** (`ghcr.io/aquasecurity/trivy-db:2` — the tag *is* the schema version). Pulled via plain OCI Distribution HTTP: token → manifest → blob. Measured live manifest (2026-08-18):

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.aquasec.trivy.config.v1+json",
  "layers": [{
      "mediaType": "application/vnd.aquasec.trivy.db.layer.v1.tar+gzip",
      "digest": "sha256:6954c405f78c9bc0f1c2192e82645b38326decf7ec53f420af3688ad30b949db",
      "size": 113381933,
      "annotations": {"org.opencontainers.image.title": "db.tar.gz"}
  }],
  "annotations": {"org.opencontainers.image.created": "2026-08-18T06:55:43Z"}
}
```

- **Measured full-DB size: 113,381,933 bytes ≈ 113 MB** gzip'd (BoltDB inside; roughly 600–700 MB unpacked).
- Cadence: DB built **every 6 hours**; client honors a `NextUpdate` field in embedded metadata (default update interval 24 h) and skips re-download if the local DB is <1 h old. Content-addressed digests give free dedup/caching.
- Delta strategy: none — full replacement. GHCR rate-limiting of the whole community became a recurring operational pain (multiple issues; mirrors recommended) — a lesson: **per-customer auth (which ZenPlus has) makes capacity planning sane**.
- Air-gap story: `oras pull ghcr.io/aquasecurity/trivy-db:2` on a connected machine, move `db.tar.gz`, unpack into the cache dir, run with `--skip-db-update`. I.e. *the offline flow is the same artifact, hand-carried* — no special build.

## 4. Case study: Tenable Nessus plugin feed (online + offline)

Sources: [Update Plugins Offline (Nessus 10.x)](https://docs.tenable.com/nessus/Content/UpdatePluginsOffline.htm), [Tenable plugins page](https://www.tenable.com/plugins), [avleonov offline walkthrough](https://avleonov.com/2020/10/25/nessus-essentials-with-offline-registration-and-plugin-updates/), [Cibermanchego automation](http://cibermanchego.com/en/post/2022-02-13-automate-offline-tenable-plugin-updates/).

- Online: every scanner checks Tenable's plugin service **every 24 h** and pulls compiled plugin updates automatically; entitlement via the activation code bound at registration.
- Offline: at offline-registration time the scanner emits a **challenge code**; the customer enters challenge + activation code on Tenable's site and receives (a) a license `nessus.license` file and (b) a **custom URL** of the form `https://plugins.nessus.org/v2/nessus.php?f=all-2.0.tar.gz&u=<user-hash>&p=<pass-hash>` valid ~1 year. Any internet-connected machine fetches `all-2.0.tar.gz` (the *entire* plugin set; hundreds of MB → multi-GB range today — Tenable Research publishes ~430k plugins covering ~146k CVE IDs as of 2026), then it's imported via UI upload or `nessuscli update all-2.0.tar.gz`.
- Delta option: swapping the filename in the custom URL to `sc-plugins-diff.tar.gz` yields a **differential archive** (Security Center flavor) — a named-file delta, not an offset protocol.
- Architectural takeaways for ZenPlus: (1) the *pre-authorized URL keyed to the license* is a clean air-gap UX (no interactive login needed on the fetching machine); (2) full + named-diff artifacts on the same endpoint; (3) a January-2025 incident where a **faulty differential plugin update crashed agents fleet-wide** ([securityaffairs](https://securityaffairs.com/172738/security/nessus-scanner-agents-issue.html)) argues for staged rollout + a validation gate before publishing (as Grype does).

## 5. Case study: Greenbone feed sync (rsync)

Sources: [greenbone-feed-sync repo](https://github.com/greenbone/greenbone-feed-sync), [Greenbone feed-sync docs](https://greenbone.github.io/docs/latest/22.4/source-build/feed-sync.html).

- Transport: **rsync only**, from `rsync://feed.community.greenbone.net/community`. Module paths are versioned: NASL VTs at `/vulnerability-feed/$FEED_VERSION/vt-data/nasl/`, notus at `/vulnerability-feed/$FEED_VERSION/vt-data/notus/`, SCAP at `/vulnerability-feed/$FEED_VERSION/scap-data`, CERT at `/cert-data`, gvmd data objects at `/data-feed/$FEED_VERSION/` (`$FEED_VERSION` default `25.0` — version-in-the-path is their schema-compatibility gate).
- Feed content classes: VT/NASL scripts, notus (JSON package-version advisories), SCAP (CPE/CVE), CERT advisories, gvmd data objects (scan configs, port lists, report formats) — i.e. *detection logic and data ship in one feed tree*.
- Delta strategy: rsync's block-level algorithm gives incremental transfer for free; the on-disk tree is *the* database input (gvmd/openvas re-ingest changed files). Locking via `/var/lib/gvm/feed-update.lock` and `/var/lib/openvas/feed-update.lock` prevents ingest-during-sync.
- Enterprise feed: same tool, different URL, authenticated by an SSH/feed key file at `/etc/gvm/greenbone-enterprise-feed-key`.
- Community rsync is a chronic support burden (timeouts, blocked 873/tcp in corporate networks — many forum threads). **Lesson: HTTPS-only is the right call for appliances in enterprise networks; ZenPlus should not introduce a second port/protocol.**

## 6. Case study: Wazuh CTI vulnerability content — snapshot + offset deltas (best-in-class delta model)

Sources: [Wazuh VD how-it-works](https://documentation.wazuh.com/current/user-manual/capabilities/vulnerability-detection/how-it-works.html), [offline update docs](https://documentation.wazuh.com/current/user-manual/capabilities/vulnerability-detection/offline-update.html), [Introducing Wazuh CTI](https://wazuh.com/blog/introducing-wazuh-cti/), plus live probing of cti.wazuh.com.

- Central side: Wazuh CTI aggregates OS-vendor feeds (Ubuntu/Debian/RHEL/Alma/Amazon/SUSE/Windows/macOS…) + NVD, OSV, CISA, Microsoft MSRC, normalizes everything to **CVE JSON 5.x** via a "Vulnerability Detector Provider", and publishes per-consumer *contexts*.
- Catalog/discovery API, measured live 2026-08-18 — `GET https://cti.wazuh.com/api/v1/catalog/contexts/vd_1.0.0/consumers/vd_4.8.0`:

```json
{"data": {
    "name": "vd_4.8.0",
    "context": "vd_1.0.0",
    "changes_url": "cti.wazuh.com/api/v1/catalog/contexts/vd_1.0.0/consumers/vd_4.8.0/changes",
    "last_offset": 3798262,
    "last_snapshot_at": "2026-08-17T10:51:44Z",
    "last_snapshot_link": "https://cti.wazuh.com/store/contexts/vd_1.0.0/consumers/vd_4.8.0/3792577_1786963904.zip",
    "last_snapshot_offset": 3792577
}}
```

- **Measured snapshot size: 311,592,223 bytes ≈ 312 MB zip** (the full multi-OS CVE content for the consumer). Snapshot filename encodes `<offset>_<unix-ts>.zip`.
- Delta protocol: every content mutation gets a monotonically increasing **offset** (like a Kafka log). Client bootstrap = download snapshot at offset N; steady-state = `GET …/changes?from_offset=N&to_offset=M` (both params required — verified live: omitting `to_offset` returns `{"errors":{"to_offset":["can't be blank"]}}`) returning JSON change records to apply. Today's gap between snapshot (3,792,577) and head (3,798,262) was ~5.7k offsets — i.e. snapshots are rebuilt roughly daily and deltas number a few thousand records/day *for a whole multi-OS corpus*.
- The context (`vd_1.0.0`) and consumer (`vd_4.8.0`) version the content schema independently of the product; `paths_filter`/`paths_reject` fields exist for server-side slicing.
- Offline: download the same snapshot zip out-of-band, drop it on the manager, point `ossec.conf` `<offline-url>` at the file — again, *the air-gap artifact is the ordinary snapshot*.
- **This snapshot+offset-log model is the recommended delta architecture for ZenPlus** — it degrades gracefully (client too far behind → just take a new snapshot) and the server stays stateless per client.

## 7. Case study: Microsoft WSUS metadata model (conceptual)

Sources: [Viewing and managing updates](https://learn.microsoft.com/en-us/windows-server/administration/windows-server-update-services/manage/viewing-and-managing-updates), Microsoft Update protocol docs (MS-WSUSSS), July-2026 metadata incident coverage ([windowsforum](https://windowsforum.com/windows-news.4/wsus-sync-delays-microsoft-repairs-july-2026-metadata-issue.439296/)).

- Separation of **metadata** vs **content**: WSUS syncs update *metadata* (title, classification, product, EULA, detection/applicability rules, supersedence edges) from Microsoft; binary payloads download separately/lazily. Appliance analog: sync CVE/patch metadata always; fetch bulky remediation artifacts only if ever needed.
- **Revisions**: any metadata change to an update creates a new *revision* of the same UpdateID; clients/downstream servers re-sync only changed revisions (`(UpdateID, RevisionNumber)` pairs). A July-2026 upstream bug that re-issued revisions for thousands of old updates melted downstream WSUS servers — a caution about mass-touch operations in a delta feed (a re-normalization that bumps every record forces every appliance to re-download the world; version such changes as a *new snapshot*, not a delta storm).
- **Supersedence graph**: a directed edge set "update A supersedes update B" (transitive; effectively a DAG per product). Consumers use it to (a) hide/decline superseded patches, (b) compute "the newest patch that fixes CVE X on product Y". ZenPlus patch recommendation needs exactly this: `patch_supersedes(patch_id, superseded_patch_id)` edge table, and "recommended patch" = sinks of the graph restricted to patches fixing the asset's open CVEs.
- **Approval workflow**: updates are synced but inert until an admin approves per target group — maps to a curation/approval step in the zentryc pipeline and optionally per-customer suppression lists.

## 8. Vendor advisory distribution: CSAF 2.0 (ingest side for PSIRTs)

Sources: [OASIS CSAF 2.0 spec §7](https://docs.oasis-open.org/csaf/csaf/v2.0/os/csaf-v2.0-os.html), [Cisco CSAF note](https://community.cisco.com/t5/devnet-general-knowledge-base/what-is-the-common-security-advisory-framework-csaf/ta-p/4743320), [Greenbone CSAF roles blog](https://www.greenbone.net/en/blog/understanding-csaf-2-0-stakeholders-and-roles/).

- CSAF 2.0 is the standardized machine-readable advisory format + **distribution requirements**: a publisher exposes `https://<domain>/.well-known/csaf/provider-metadata.json` (discoverable via RFC 9116 `security.txt`), then serves advisories either **directory-based** (year-partitioned dirs + `index.txt`/`changes.csv`) or **ROLIE-based** (Atom-ish JSON feeds per TLP tier). Cisco: `https://www.cisco.com/.well-known/csaf/provider-metadata.json`.
- Cisco is deprecating CVRF in favor of CSAF; Juniper, Palo Alto, Fortinet, Siemens, Red Hat, SUSE all publish CSAF. The BSI-funded `gocsaf/csaf` tooling (`csaf_downloader`, aggregator) can bulk-mirror any conformant publisher.
- Cisco PSIRT **openVuln API** (for richer querying): OAuth2 client-credentials on cisco.com account; rate limit **5 calls/s, 30/min, 5,000/day**; `pageSize` max 100; endpoints per product family and notably `/security/advisories/v2/OS_version/OS_data?OSType=ios&version=15.2(4)M` style queries mapping *exact OS versions* → advisories, plus first-fixed info. ([developer.cisco.com/docs/psirt](https://developer.cisco.com/docs/psirt/)). This is the highest-precision source for Cisco firmware matching (much better than CPE-matching NVD for IOS/IOS-XE).
- **Takeaway:** the zentryc pipeline should ingest network-vendor CSAF/PSIRT sources centrally (credentials, rate limits, format wrangling live in one place) and re-publish a normalized slice — appliances never talk to vendor APIs.

## 9. Upstream corpus sources & mirroring strategy (2026 reality)

### 9.1 NVD

- Legacy JSON 1.1 data feeds (`nvdcve-1.1-<year>.json.gz`, "modified", "recent") were **deprecated 2023-12-15 and fully retired**; the API is the only NIST-native channel ([change timeline](https://nvd.nist.gov/general/news/change-timeline), [retirement update](https://groups.google.com/a/list.nist.gov/g/nvd-news/c/aofnAd3HP2g)).
- NVD API 2.0: `https://services.nvd.nist.gov/rest/json/cves/2.0`, `resultsPerPage` ≤ **2000**, rate limit **5 req/rolling-30 s without key, 50 with free key**; incremental sync via `lastModStartDate`/`lastModEndDate` (range ≤ 120 days). NIST's own guidance: after an initial full paginated pull, polling the modified window **every ≥ 2 h** keeps a mirror current within the rate limit ([api-workflows](https://nvd.nist.gov/developers/api-workflows)).
- **Measured live 2026-08-18: `totalResults: 378,754` CVEs.** At 2000/page that's ~190 requests ≈ 8–10 min for a full pull with a key.
- Since June 2026, NVD records can carry CISA **SSVC/ADP ("Vulnrichment")** decision data.
- Community fallback mirrors: [fkie-cad/nvd-json-data-feeds](https://github.com/fkie-cad/nvd-json-data-feeds) rebuilds the legacy per-year JSON feeds daily at 00:00 UTC from the API (useful as a bulk bootstrap without pagination); VulnCheck **NVD++** offers free re-hosted bulk NVD 2.0 data.

### 9.2 cvelistV5 (CVE Program, GitHub)

[CVEProject/cvelistV5](https://github.com/CVEProject/cvelistV5) publishes CVE Record Format 5.x JSON via **GitHub Releases**: a daily **baseline** `YYYY-MM-DD_all_CVEs_at_midnight.zip` (unchanged for 24 h) plus **hourly deltas** `YYYY-MM-DD_delta_CVEs_at_HH00Z.zip`; repo itself updates ~every 7 min and can be `git pull`ed. **Measured live (release `cve_2026-08-18_0900Z`): baseline zip = 580.4 MB; the 09:00Z hourly delta = 0.3 MB.** That ratio (full : hourly delta ≈ 2000 : 1) is the canonical argument for baseline+delta distribution. Per-record compressed cost ≈ 580 MB / 378.8k ≈ **1.5 KB/CVE**.

### 9.3 OSV

`gs://osv-vulnerabilities` GCS bucket, HTTP at `https://storage.googleapis.com/osv-vulnerabilities/…`: per-ecosystem `<ECOSYSTEM>/all.zip` plus a global `all.zip`. **Measured live: global `all.zip` = 1,503,645,445 bytes ≈ 1.5 GB.** OSV matters for the *server software inventory* half (distro packages: `Ubuntu/all.zip`, `Debian/all.zip`, `Red Hat`…, each tens of MB), giving distro-corrected affected/fixed **version ranges** that NVD CPE data lacks. ([OSV data docs](https://google.github.io/osv.dev/data/))

### 9.4 KEV, EPSS, EOL

- **CISA KEV**: `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` — measured `ETag: "182f16-…"` ⇒ ~1.58 MB; ~1.4k entries; updated a few times/week; serves `Last-Modified`/`ETag` (conditional GET friendly). Mirror: [cisagov/kev-data](https://github.com/cisagov/kev-data).
- **EPSS**: daily scores for every CVE. `https://epss.empiricalsecurity.com/epss_scores-current.csv.gz` (302 → `epss_scores-YYYY-MM-DD.csv.gz`; FIRST moved hosting to Empirical Security). **Measured: 2,546,842 bytes ≈ 2.5 MB gz/day** (CSV: `cve,epss,percentile`). ([first.org/epss/data](https://www.first.org/epss/data))
- **endoflife.date**: v1 API, measured live: `https://endoflife.date/api/v1/products/` → **464 products** (`schema_version: 1.2.1`); per-product `GET /api/v1/products/{name}/` returns releases with `eol`/`eoas`/`eoes` dates **and CPE identifiers**, e.g. Ubuntu carries `{"type":"cpe","id":"cpe:2.3:o:canonical:ubuntu_linux"}` — directly joinable to CVE CPEs and to device OS fingerprints. Full dump is a few MB. Network-gear EOL beyond its coverage: Cisco **EOX API** (Support API suite, per-PID hardware/software EOL dates, OAuth2).

### 9.5 Anti-hammering norms

Everyone converges on: **one central mirror pulls the rate-limited sources; edge nodes pull only from the mirror.** For ZenPlus this is not optional — appliances sit in customer networks that may block arbitrary egress; the appliance contract should be "talks HTTPS to zentryc.com only" (the same constraint that already exists for OTA). Public NTP is already blocked on this very appliance (see MEMORY: server clock via core switch) — assume the worst about customer egress.

---

## 10. Size & cadence reference table (measured 2026-08-18 unless noted)

| Corpus / artifact | Full size (compressed) | Delta size | Cadence |
|---|---|---|---|
| cvelistV5 baseline (all 378,754 CVEs, JSON 5.x) | 580.4 MB zip | 0.3 MB/hour | daily baseline + hourly deltas |
| NVD via API 2.0 | ~190 paged requests (2000/page) | `lastModStartDate` window | poll ≥ every 2 h |
| OSV global all.zip | 1.50 GB | per-ecosystem zips; date-modified index | continuous export |
| Grype DB v6 (SQLite, tar.zst) | 145.1 MB (was 65 MB at v6 launch) | none (full replace) | daily |
| Trivy DB (BoltDB, tar.gz OCI layer) | 113.4 MB | none (full replace) | built every 6 h; clients daily |
| Wazuh CTI vd consumer snapshot | 311.6 MB zip | offset-log `changes` API, ~5–6k records/day | snapshot ~daily; deltas continuous |
| Nessus all-2.0.tar.gz (~430k plugins) | multi-GB class (not measured; grew from ~240 MiB circa 2020) | `sc-plugins-diff.tar.gz` | daily; client checks every 24 h |
| CISA KEV JSON | ~1.6 MB | n/a (tiny; conditional GET) | few times/week |
| EPSS scores CSV | 2.5 MB gz/day | n/a (full daily file) | daily |
| endoflife.date full product dump | few MB | n/a | on change; weekly pull is fine |
| **ZenPlus curated slice (projected, §12.3)** | **10–40 MB zstd** | **≤ 1 MB/day** | daily snapshot + 6 h deltas |

---

## 11. Recommended architecture — central server (zentryc.com Django)

### 11.1 Content pipeline (ingest → normalize → curate → publish)

```
[NVD API 2.0]──┐
[cvelistV5]────┤            ┌────────────┐   ┌─────────────┐   ┌──────────────────────┐
[KEV json]─────┼─ ingestors →│ raw_store  │→  │ normalizer/ │→  │ curated corpus (PG)  │
[EPSS csv]─────┤  (celery/   │ (per-source│   │ merger      │   │ + monotonic change   │
[OSV zips]─────┤   cron)     │  versioned)│   │ (CVE JSON5  │   │   log (offset)       │
[vendor CSAF]──┤            └────────────┘   │  canonical)  │   └─────────┬────────────┘
[Cisco openVuln]┤                             └─────────────┘             │ publisher (daily/6h)
[endoflife.date]┘                                                        ▼
                                              snapshots + deltas + manifest.json + manifest.sig (Ed25519)
                                              → static files behind nginx (or R2/S3), immutable names
```

1. **Ingestors** (one scheduled job per source, independent failure domains): NVD modified-window poll every 2 h with API key; cvelistV5 hourly delta zips (cheap redundancy/cross-check for NVD lag); KEV + EPSS daily; OSV per-ecosystem zips daily for the distros ZenPlus servers run; vendor CSAF (Cisco/Juniper/Fortinet/PAN/Aruba…) via `provider-metadata.json` discovery daily; Cisco openVuln weekly per-OS-version sweeps (5k calls/day budget); endoflife.date weekly. Keep the **raw pull** archived (source, fetch time, bytes) for reproducibility.
2. **Normalizer**: canonical internal record ≈ CVE JSON 5.x (the Wazuh precedent) with merged enrichments: CVSS (prefer CNA, fall back NVD, keep SSVC/ADP), EPSS score+percentile, KEV flag+due-date, affected-product ranges (CPE ranges from NVD **plus** vendor-precise ranges from CSAF `product_tree`/openVuln first-fixed, which win on conflict), patch/fix references, supersedence edges where derivable (Cisco first-fixed chains; MSRC supersedence).
3. **Curation gate** (WSUS "approval" + Grype "validation" lessons): the slice filter (see §12.3) + automated sanity checks before publish — record-count deltas within tolerance, schema-validate every record, refuse to publish a delta touching > X% of the corpus (force a snapshot instead; the WSUS July-2026 revision-storm and the Nessus bad-diff incident both motivate this), optional human hold for the network-OS rules.
4. **Publisher**: append accepted changes to a **monotonic change log** (`feed_change(offset BIGSERIAL, cve_id, op ENUM('upsert','delete'), payload JSONB)`), then cut artifacts:
   - `snapshot`: full curated corpus at offset N → JSONL (one canonical record per line) → `zstd` → `snapshot_<channel>_v<schema>_<offset>_<builtISO>.tar.zst`
   - `delta`: all changes (N₀, N₁] → same JSONL form → `delta_<channel>_v<schema>_<from>_<to>.jsonl.zst`
   - `manifest.json` (small, mutable pointer — the Grype `latest.json` idea, extended) + `manifest.sig` (Ed25519 detached, **feed-signing key distinct from the release key**; appliance ships `zentryc-feed.pub` next to `zentryc-release.pub` in `/opt/zenplus/updater/keys/`).

### 11.2 Channel/manifest layout (proposed, concrete)

Static tree (nginx `alias`, immutable cache headers on artifacts, 5-min cache on manifests):

```
/feeds/vuln/v1/<channel>/manifest.json          # mutable pointer, tiny
/feeds/vuln/v1/<channel>/manifest.sig
/feeds/vuln/v1/<channel>/snapshot_<...>.tar.zst  # immutable
/feeds/vuln/v1/<channel>/delta_<...>.jsonl.zst   # immutable, keep ~30 days
```

Channels: `network-server` (default curated slice), later `full` if ever needed. `v1` = feed schema major (Greenbone's version-in-path).

`manifest.json` (signed bytes = this exact file):

```json
{
  "schema_version": "1.0.0",
  "channel": "network-server",
  "status": "active",
  "built": "2026-08-18T06:00:00Z",
  "head_offset": 184223,
  "snapshot": {
    "path": "snapshot_network-server_v1_183900_2026-08-18T00:00:00Z.tar.zst",
    "offset": 183900,
    "sha256": "…",
    "size": 23456789
  },
  "deltas": [
    {"path": "delta_…_183900_184100.jsonl.zst", "from_offset": 183900, "to_offset": 184100, "sha256": "…", "size": 81234},
    {"path": "delta_…_184100_184223.jsonl.zst", "from_offset": 184100, "to_offset": 184223, "sha256": "…", "size": 40210}
  ],
  "aux": {
    "epss": {"path": "epss_2026-08-18.csv.zst", "sha256": "…", "size": 2400000},
    "eol":  {"path": "eol_2026-08-17.json.zst", "sha256": "…", "size": 350000}
  }
}
```

Freshness: appliance re-uses `crypto.verify_manifest`-style checks with a **feed `max_age_days` of 7** (a feed 30 days stale is itself a security finding; surface it as an alert, mirroring `SecurityConfig.max_manifest_age_days` at `/opt/zenplus/updater/config.py:30`).

### 11.3 Django API endpoints (consistent with existing `/api/v1/…`)

- `GET /api/v1/vulnfeed/manifest?channel=network-server` — returns manifest + sig inline; auth = existing `Authorization: Bearer <api_key>` + `X-Appliance-ID` (`agent.py:71–87`); server checks subscription entitlement (`compliance_module: true` in the subscription JSON) and logs per-appliance feed telemetry (fleet freshness dashboard).
- `GET /feeds/vuln/v1/<channel>/<artifact>` — static, nginx-served, `Accept-Ranges: bytes`, `ETag`, `Cache-Control: public, max-age=31536000, immutable` for artifacts; the existing `download_package()` consumes this unchanged.
- `POST /api/v1/vulnfeed/report` — `{applied_offset, snapshot_offset, schema_version, duration_ms, status, error_message}` (mirror of `/api/v1/updates/report`, `agent.py:265`).
- `GET /api/v1/vulnfeed/bundle` — **air-gap**: returns a single `.zvb` tar (`manifest.json` + `manifest.sig` + snapshot + aux files). Portal variant: a **pre-authorized tokenized URL shown on the customer's zentryc subscription page** (Nessus custom-URL pattern) so any connected workstation can `curl` it without portal login; dashboard gains "Upload feed bundle" (same verify path as network sync). Bundle import must enforce signature + freshness + `snapshot.offset >= local offset` (no rollback).

Django models sketch: `FeedChannel(name, schema_version, status)`, `FeedChange(offset PK, cve_id, op, payload, source, created_at)`, `FeedArtifact(channel, kind ENUM('snapshot','delta','aux'), path, from_offset, to_offset, sha256, size, built_at)`, `ApplianceFeedStatus(appliance_id, applied_offset, schema_version, reported_at)`.

---

## 12. Recommended architecture — appliance side

### 12.1 Sync protocol

New `updater/`-style loop (or a FastAPI background task in `/opt/zenplus/server`), every **6 h with ±30 min jitter** (Trivy cadence; hourly is overkill for on-prem patching workflows, daily is too slow for KEV):

1. `GET /api/v1/vulnfeed/manifest?channel=…` with `If-None-Match: <cached ETag>` → 304 short-circuits the whole cycle (KEV's own hosting demonstrates the pattern).
2. Verify Ed25519 sig + freshness + `schema_version` major compatibility (refuse newer majors → raise "update appliance" banner, Grype `status`/schema model).
3. If `head_offset == local applied_offset` → done. If local offset ≥ some delta's `from_offset` chain start → download only needed deltas (a few hundred KB). If local offset predates the delta window, or sha mismatch, or first run → snapshot + trailing deltas (Wazuh degrade-to-snapshot).
4. Download via existing `download_package()` (sha256 + Range resume, `downloader.py:20`).
5. Apply transactionally to Postgres; write `applied_offset` only in the same transaction; then re-run the matching engine on changed CVEs only; `POST /api/v1/vulnfeed/report`.

### 12.2 Storage format on the appliance: Postgres tables (recommended) over sqlite blob

- **SQLite-blob** (Grype/Trivy style: swap a whole file atomically) suits stateless CLI scanners. ZenPlus is different: matches must **join against live Postgres inventory** (devices, `udt_device_settings`-style tables, server software inventory), feed the report engine, RBAC, and alerting — all Postgres-native. Running a second query engine and cross-DB joins in Python would be the worst of both worlds.
- **Recommendation:** distribute JSONL (portable, diffable, schema-versioned), **load into Postgres** on the appliance; keep the last verified snapshot archive on disk (`/opt/zenplus/updater/feeds/`) for rollback/reload — that file *is* the "sqlite blob" durability story without the second engine.
- Migration discipline: feed tables ship via a normal numbered Postgres migration (probe-able objects — see MEMORY "Migrations must be classifiable"); feed *content* never ships in migrations.

Proposed DDL sketch (names align with existing snake_case conventions):

```sql
CREATE TABLE vuln_definitions (
  cve_id            TEXT PRIMARY KEY,          -- 'CVE-2026-12345'
  title             TEXT,
  description       TEXT,
  cvss_version      TEXT,                      -- '3.1' | '4.0'
  cvss_score        NUMERIC(3,1),
  cvss_vector       TEXT,
  severity          TEXT,                      -- critical/high/medium/low
  epss_score        NUMERIC(6,5),
  epss_percentile   NUMERIC(6,5),
  kev               BOOLEAN NOT NULL DEFAULT FALSE,
  kev_due_date      DATE,
  published_at      TIMESTAMPTZ,
  modified_at       TIMESTAMPTZ,
  source_offset     BIGINT NOT NULL,           -- feed offset that last touched this row
  raw               JSONB NOT NULL             -- canonical record (CVE JSON5-ish)
);
CREATE TABLE vuln_affects (                    -- exploded matching rows
  id            BIGSERIAL PRIMARY KEY,
  cve_id        TEXT NOT NULL REFERENCES vuln_definitions ON DELETE CASCADE,
  match_kind    TEXT NOT NULL,                 -- 'cpe' | 'vendor_os' | 'distro_pkg'
  vendor        TEXT, product TEXT,            -- normalized (e.g. 'cisco','ios_xe')
  version_start TEXT, version_start_incl BOOLEAN,
  version_end   TEXT, version_end_incl   BOOLEAN,
  fixed_in      TEXT,                          -- first-fixed version if known
  cpe23         TEXT                           -- original cpe:2.3 string when match_kind='cpe'
);
CREATE INDEX ON vuln_affects (vendor, product);
CREATE TABLE eol_products (
  id BIGSERIAL PRIMARY KEY, product TEXT, cycle TEXT,
  cpe23_prefix TEXT, eol_date DATE, eoes_date DATE, latest_release TEXT
);
CREATE TABLE vuln_feed_state (                 -- single row
  channel TEXT PRIMARY KEY, schema_version TEXT,
  applied_offset BIGINT NOT NULL, snapshot_offset BIGINT,
  last_sync_at TIMESTAMPTZ, last_manifest_etag TEXT, last_error TEXT
);
```

(Asset-match results — `vuln_findings(device_id/server_id, cve_id, state, first_seen, resolved_at)` — belong to the matching-engine investigator, but `vuln_affects.vendor/product` is the join surface they'll consume.)

### 12.3 Curated slice sizing (why appliances must NOT get the full corpus)

Full corpus ≈ 378.8k CVEs ≈ 580 MB zip (measured, §9.2) — pointless on an appliance that monitors network gear + servers. The zentryc curation filter keeps a CVE iff any affected product matches: network-OS vendors (Cisco IOS/IOS-XE/NX-OS/ASA/FTD, Juniper Junos, Aruba/HPE, Fortinet FortiOS, PAN-OS, Arista EOS, MikroTik RouterOS, Ubiquiti, common switch/AP/firewall/UPS/printer vendors), server OSes (Windows Server, RHEL/Alma/Rocky, Ubuntu, Debian, SUSE, VMware ESXi), and server software present in ZenPlus inventory categories. Estimate: **15–25% of the corpus ⇒ ~60–95k CVEs.** At cvelistV5's ~1.5 KB/CVE compressed that's 90–145 MB if records ship verbatim; with normalized minimal records (drop CNA boilerplate, keep the §12.2 fields + top references, ~1–2 KB raw JSON/record, zstd ~5–8×) the snapshot lands at **~10–40 MB** — a 30-second download on a slow office uplink, monthly transfer < 100 MB/appliance even with daily snapshots, vs 17 GB/month if the full corpus were shipped daily. Daily churn (new+modified CVEs intersecting the slice) is a few hundred records ⇒ **deltas < 1 MB/day**; EPSS aux file (slice-filtered, scores only for CVEs in the slice) ~0.5 MB/day.

### 12.4 Cadence summary (recommended)

| Path | Cadence |
|---|---|
| zentryc ← NVD API (modified window) | every 2 h |
| zentryc ← cvelistV5 hourly deltas | hourly (cross-check/lag fill) |
| zentryc ← KEV / EPSS / OSV distro zips | daily (KEV also event-driven if webhook/RSS added) |
| zentryc ← vendor CSAF, Cisco openVuln | daily / weekly sweep |
| zentryc ← endoflife.date | weekly |
| publish delta artifacts | every 6 h (only if changes) |
| publish snapshot | daily (00:00 UTC build, Grype/cvelistV5 convention) |
| appliance → manifest poll | every 6 h + jitter; conditional GET |
| appliance stale-feed alert | > 7 days without successful sync |

---

## 13. Key design decisions distilled (for the plan writer)

1. **Tiny signed mutable manifest + immutable content-addressed artifacts** (Grype v6 `latest.json`, atomically published) — never mutate artifacts in place; never let clients parse a growing listing.
2. **Snapshot + monotonic-offset delta log with degrade-to-snapshot** (Wazuh CTI; cvelistV5 baseline/hourly-delta is the same shape) — server stateless per client, delta chain window ~30 days.
3. **HTTPS only, one hostname** (Greenbone's rsync pain; ZenPlus appliances already assume restricted egress).
4. **Reuse the OTA trust + transport stack verbatim** — Bearer/`X-Appliance-ID` auth (`agent.py:71`), Ed25519 detached manifest sig + max-age freshness (`crypto.py:126`), sha256+Range downloader (`downloader.py:20`) — with a *separate feed signing key* and feed `max_age_days≈7`.
5. **Centralize all upstream credentials/rate limits** (NVD key, Cisco OAuth2, CSAF crawling) on zentryc; appliances never call third parties.
6. **Curate hard**: a network+server slice is ~10–40 MB vs 580 MB/1.5 GB full corpora; ship precision vendor ranges (CSAF/openVuln) not just NVD CPEs.
7. **Validation gate + snapshot-not-delta for mass changes** (Grype match-label validation; Nessus Jan-2025 bad-diff outage; WSUS July-2026 revision storm).
8. **Postgres storage on the appliance, JSONL on the wire**, retained snapshot archive as the rollback blob; supersedence as an edge table for patch recommendation (WSUS model).
9. **Air-gap = the same signed snapshot bundle**, obtainable via a license-keyed pre-authorized URL (Nessus challenge-code UX) and importable through the dashboard with identical verification; offset monotonicity blocks rollback.
10. **Fleet telemetry**: appliances report applied offset (like `/api/v1/updates/report`), so zentryc can dashboard fleet feed freshness and page on stragglers.

## 14. Sources

- https://oss.anchore.com/docs/architecture/grype-db/ ; https://anchore.com/blog/grype-db-schema-evolution-from-v5-to-v6-smaller-faster-better/ ; https://grype.anchore.io/databases/v6/latest.json (live)
- https://github.com/aquasecurity/trivy-db ; https://trivy.dev/docs/v0.57/guide/advanced/air-gap/ ; ghcr.io/v2/aquasecurity/trivy-db manifest (live)
- https://docs.tenable.com/nessus/Content/UpdatePluginsOffline.htm ; https://www.tenable.com/plugins ; https://avleonov.com/2020/10/25/nessus-essentials-with-offline-registration-and-plugin-updates/ ; http://cibermanchego.com/en/post/2022-02-13-automate-offline-tenable-plugin-updates/ ; https://securityaffairs.com/172738/security/nessus-scanner-agents-issue.html
- https://github.com/greenbone/greenbone-feed-sync ; https://greenbone.github.io/docs/latest/22.4/source-build/feed-sync.html
- https://documentation.wazuh.com/current/user-manual/capabilities/vulnerability-detection/how-it-works.html ; …/offline-update.html ; https://wazuh.com/blog/introducing-wazuh-cti/ ; https://cti.wazuh.com/api/v1/catalog/contexts/vd_1.0.0/consumers/vd_4.8.0 (live)
- https://learn.microsoft.com/en-us/windows-server/administration/windows-server-update-services/manage/viewing-and-managing-updates ; https://windowsforum.com/windows-news.4/wsus-sync-delays-microsoft-repairs-july-2026-metadata-issue.439296/
- https://docs.oasis-open.org/csaf/csaf/v2.0/os/csaf-v2.0-os.html ; https://developer.cisco.com/docs/psirt/ ; https://community.cisco.com/t5/devnet-general-knowledge-base/what-is-the-common-security-advisory-framework-csaf/ta-p/4743320
- https://nvd.nist.gov/general/news/change-timeline ; https://nvd.nist.gov/developers/api-workflows ; https://groups.google.com/a/list.nist.gov/g/nvd-news/c/aofnAd3HP2g ; https://github.com/fkie-cad/nvd-json-data-feeds ; https://www.vulncheck.com/blog/nvd-plus-plus ; services.nvd.nist.gov CVE API (live)
- https://github.com/CVEProject/cvelistV5 (+ GitHub Releases API, live)
- https://google.github.io/osv.dev/data/ ; https://storage.googleapis.com/osv-vulnerabilities/all.zip (live HEAD)
- https://www.first.org/epss/data ; https://epss.empiricalsecurity.com/epss_scores-current.csv.gz (live) ; https://www.cisa.gov/known-exploited-vulnerabilities-catalog ; KEV JSON feed (live HEAD) ; https://github.com/cisagov/kev-data
- https://endoflife.date/api/v1/ (live)
