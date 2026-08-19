# 06 — Zentryc Feed Service Specification (central server)

*Status: Design proposal · 2026-08-18. Scope: everything that runs on zentryc.com to produce and serve the vulnerability/patch/EOL feed. Companion: doc 03 §1–2 (appliance side), `raw/research-cve-sources.md`, `raw/research-feed-distribution.md`, `raw/code-zentryc-sync.md`.*

---

## 1. Operational reality & deployment shape

zentryc.com today is a hand-deployed Django/DRF app (no git on the host, 187.77.177.190, gunicorn `zentryc.service` + `zentryc-celery.service`, Cloudflare-fronted) that serves the OTA release API, licensing, and the KB. Two consequences:

1. **Minimize novel code on the existing app.** The appliance-facing surface is deliberately tiny (3 endpoints + static files) and isomorphic to the existing releases implementation.
2. **The content pipeline is a separate deployable** — `zentryc-feedbuilder`, a scheduled worker (Celery beat on the same host initially, or a small separate VM) that owns all upstream ingestion and publishes artifacts to disk. It can crash, be redeployed, or fall behind without affecting appliance-facing serving (last-published artifacts keep serving). Keep the builder's code in a git repo (unlike the site) — it changes often as sources evolve.

## 2. Upstream ingestion (per-source jobs, independent failure domains)

| Source | Job cadence | Method | Notes |
|---|---|---|---|
| cvelistV5 (CVE JSON 5.x) | hourly | GitHub Releases: daily baseline zip + hourly delta zips (or `git pull`) | **Primary record + CNA `affected[]` applicability + CISA-ADP.** ~580 MB baseline, 0.3 MB/hourly delta |
| NVD API 2.0 | every 2 h | `lastModStartDate` window, API key (50 req/30 s) | CPE/CVSS for the legacy corpus; store `vulnStatus` — post-Apr-2026 most new CVEs get no NVD enrichment |
| CISA KEV | 4×/day | JSON, conditional GET | ~1.6 MB; drives `kev`, `kev_due_date`, ransomware flag |
| EPSS | daily | `epss_scores-current.csv.gz` (~2.5 MB) | Note model-version boundaries; host = epss.empiricalsecurity.com |
| Cisco openVuln | weekly sweep + on-demand | OAuth2; `OSType/{os}?version=` per fleet-observed version (cache per (ostype, version); 5k calls/day budget) | Authoritative Cisco applicability + `firstFixed`. Fleet-observed versions come from the `observed_products` field of the vulnfeed report (§4); without that telemetry a version has no Tier-A rules and Cisco matching degrades to CNA-record Tier B |
| Cisco EoX | monthly | Support-API entitlement (server-side only) | Per-PID/serial EOL milestones — pursue entitlement; ship without it initially |
| Fortinet | daily | PSIRT RSS → `psirt/cvrf/{FG-IR-ID}` CVRF XML | Clean per-product version tables; monthly bundle 2nd Tuesday |
| Palo Alto | daily | `security.paloaltonetworks.com/api/v1/products/PAN-OS/{ver}/advisories` + advisory JSON (CSAF-shaped) | Beta API — wrap defensively; hotfix (`-hN`) aware |
| HPE Aruba | daily | CSAF directory (`csaf.arubanetworking.hpe.com`, `changes.csv` incremental) | Standard CSAF provider; use `gocsaf` tooling |
| Juniper / Ubiquiti / MikroTik | daily | cvelistV5 CNA `affected[]` (Juniper, Ubiquiti are CNAs); changelog/bulletin scrape + curation (MikroTik) | No vendor APIs — curation queue does the rest |
| MSRC | monthly + revision polls | CVRF v3 JSON per month; CSAF supplement | `ProductTree` + `Remediations` (KB, `FixedBuild`, `Supercedence`) |
| Ubuntu / Debian / RHEL / Alpine / SUSE | daily | Ubuntu OSV/USN JSON · Debian tracker JSON · RHEL CSAF-VEX (`changes.csv`) · Alpine secdb · SUSE CSAF | Ground truth for distro packages (backport-aware) |
| endoflife.date | weekly | API v1 (`/api/v1/products/…`) | Covers ios-xe, fortios, panos, routeros, windows[-server], ubuntu/rhel/debian; gaps (classic IOS, NX-OS, Junos, ArubaOS, UniFi) → curated table (+ upstream PRs) |
| Dictionaries | continuous | Curated in-repo content: sysObjectID→vendor/os_family, product aliases (seeded from winget manifests' `AppsAndFeaturesEntries` + Wazuh CPE-helper style entries) | The `unmatched_software` field of the vulnfeed report (§4) becomes the curation backlog |

Every raw pull is archived (source, fetch time, bytes, checksum) for reproducibility. Licensing posture per source is documented in `raw/research-cve-sources.md` §9 — KEV/Vulnrichment CC0, CVE ToU redistribution-friendly, EPSS/GHSA attribute, **Cisco openVuln re-serving needs legal review** (mitigation: serve derived match rules for fleet-observed versions rather than republishing the corpus).

## 3. Normalize → curate → validate → publish

### 3.1 Canonical record

Internal canonical record ≈ CVE JSON 5.x (Wazuh precedent) with merged enrichment; precedence on conflicts:

- Severity/CVSS: CNA > CISA-ADP > NVD > vendor-mapped.
- Applicability: vendor PSIRT/version-query > vendor CSAF/CVRF product_tree > CNA `affected[]` > NVD CPE configurations > keyword heuristics (emitted only as Tier-D/E rules).
- Fix data: vendor `firstFixed`/remediations > distro `fixed_version` > NVD range end.

Applicability compiles into the **`vuln_affects` row shape** the appliance consumes (doc 03 §3.1): normalized (vendor, product, train, version_scheme, ranges/exact sets, fixed_in, platform_scope, source_feed). Version-string normalizers per vendor grammar live here, once, tested centrally (`cisco_ios`, `junos`, `pan`, `semverish`, `dpkg`, `rpm`, `apk`, `win_build`).

### 3.2 Curation slice

The `network-server` channel keeps a CVE iff any affected product matches the slice: network OS vendors (Cisco IOS/IOS-XE/NX-OS/ASA/FTD, Junos, FortiOS, PAN-OS, ArubaOS/AOS-CX, EOS, RouterOS, UniFi, common switch/AP/firewall vendors), server OSes (Windows/Windows Server, Ubuntu, Debian, RHEL/Alma/Rocky, SUSE, ESXi), and application products present in the alias dictionary. Projection: **15–25% of the ~379k corpus → ~60–95k CVEs → 10–40 MB zstd snapshot, < 1 MB/day deltas** (measured baselines in `raw/research-feed-distribution.md` §10).

### 3.3 Validation gate (publish blocker)

Before any publish: schema-validate every record; record-count delta within tolerance vs previous build; **a delta touching > 5% of the corpus is refused and forces a snapshot** (WSUS July-2026 revision-storm and Nessus Jan-2025 bad-diff incidents are the cautionary tales); comparator round-trip tests on a pinned fixture set of real version strings; optional human hold for network-OS rule changes. A failed gate keeps the previous artifacts serving.

### 3.4 Publisher

Accepted changes append to a monotonic change log; artifacts are cut and written to the static tree:

```
/feeds/vuln/v1/network-server/manifest.json      # tiny mutable pointer (5-min cache)
/feeds/vuln/v1/network-server/manifest.sig       # Ed25519 detached (feed key)
/feeds/vuln/v1/network-server/snapshot_<offset>_<builtISO>.tar.zst    # immutable, daily
/feeds/vuln/v1/network-server/delta_<from>_<to>.jsonl.zst             # immutable, 6h, kept 30 days
```

`manifest.json` fields (signed bytes = the file): `schema_version`, `channel`, `status` (`active|deprecated`), `built`, `head_offset`, `snapshot {path, offset, sha256, size}`, `deltas [{path, from_offset, to_offset, sha256, size}]`, `aux {epss, eol, dictionaries}`. Per-file sha256 **inside the signed manifest** closes the `.zup` checksums gap. Snapshot daily at 00:00 UTC; deltas every 6 h when changes exist.

**Signing:** new Ed25519 keypair `zentryc-feed.key/.pub`. The private key lives on the builder (it publishes frequently and automatedly — this is exactly why it must not be the release key, which stays cold on the build host). The public key ships to appliances inside the module's OTA release.

## 4. Appliance-facing API (Django, 3 endpoints + static)

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /api/v1/vulnfeed/manifest?channel=` | Bearer api_key + X-Appliance-ID (existing middleware) | Entitlement check (`features: ["compliance"]` on the subscription) → manifest + sig inline; ETag; logs per-appliance poll |
| `GET /feeds/vuln/v1/<channel>/<artifact>` | same | nginx-served static, `Accept-Ranges`, `Cache-Control: immutable`; download rate-limit zone (reuse the releases zone) |
| `POST /api/v1/vulnfeed/report` | same | `{applied_offset, snapshot_offset, schema_version, duration_ms, status, error_message, observed_products: [{vendor, os_family, version, count}], unmatched_software: [{name, vendor, count}] (top 200)}` → `ApplianceFeedStatus` upsert (fleet freshness dashboard) + version-coverage table (drives Cisco per-version compilation) + alias-dictionary backlog. The two telemetry fields are omitted when the appliance's `share_telemetry` is off — the endpoint must accept both shapes |
| `GET /api/v1/vulnfeed/bundle` (+ portal pre-authorized URL) | tokenized | Single `.zvb` tar for air-gap (manifest + sig + snapshot + aux) |

Django models: `FeedChannel(name, schema_version, status)` · `FeedArtifact(channel, kind, path, from_offset, to_offset, sha256, size, built_at, is_published)` · `ApplianceFeedStatus(appliance, applied_offset, schema_version, reported_at)` · curation tables in the builder's own DB.

Server-side additions to existing surfaces: subscription objects gain `features` (checkin passthrough — zero appliance change needed to transport it); the admin panel gains a feed dashboard (last build, gate results, fleet freshness).

## 5. Admin & curation workflow

- **Curation queue:** low-confidence auto-derived rules (MikroTik changelog parses, scraped Juniper fix tables, new alias candidates from fleet unmatched-inventory telemetry) land in a review queue; a human approves → they enter the next build. Everything else flows automatically.
- **Kill switch:** un-publish an artifact (appliances keep last-good); `status: deprecated` on a channel tells appliances to stop advancing and surface a notice.
- **Monitoring:** per-ingestor freshness alarms (source stale > 2× cadence), gate-failure alerts, fleet-freshness percentiles. The feed being *late* is a page; the feed being *wrong* is a sev-1 (hence the gate).

## 6. Build order (maps to roadmap CV-E1/CV-E2 in doc 07)

1. **Skeleton:** channel/manifest/signing + KEV + EPSS + endoflife.date + cvelistV5 baseline ingest with the slice filter — enough to ship real definitions + EOL and prove the pipe.
2. **Network vendors:** Fortinet CVRF, PAN API, cvelistV5-CNA (Juniper/Ubiquiti), curated MikroTik; Cisco openVuln per-version cache (fleet-driven).
3. **Servers:** MSRC CVRF pipeline; Ubuntu/Debian/RHEL/Alpine trackers; alias dictionary seeding.
4. **Hardening:** Aruba CSAF, Cisco EoX entitlement, delta publishing (snapshot-only is acceptable for v1 — 10–40 MB daily is tolerable), curation queue UI.
