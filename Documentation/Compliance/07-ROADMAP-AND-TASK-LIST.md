# 07 — Roadmap, Task List & Test Plan

*Status: Design proposal · 2026-08-18. This doc is the implementation status of record. Effort sizing: S ≤ 2 wk · M ~3–6 wk · L ~7–12 wk (2–3 engineer squad incl. poller/Go, API, dashboard, DB, and the zentryc side). Lineage: this module is roadmap epic **E4 Phase 2** (compliance + firmware EoL/CVE) extended to servers, pulled forward from Phase 4 — NCM Phase 1 is built, the server baseline engine is live, and the competitive window (`#F=2 of 15`) is open.*

---

## 1. Phases

```
CV-E0 ──► CV-E1 ──► CV-E2 ──► CV-E3 ──► CV-E4
found.    feed+     servers+  remediation, config-compliance,
& ident.  devices   triage UI EOL, reports  active verify, copilot
```

### CV-E0 — Foundations & identity hardening (S–M, no user-visible module yet)

The feed is useless against bad inventory. Ships quietly inside a normal release.

| # | Task | Area | Est (d) | Depends |
|---|---|---|---|---|
| 0.1 | `migrate-080-compliance-inventory.sql` (devices.os_family/sys_descr/identity_updated_at; servers distro_id/release/codename; software-inventory columns incl. product_code + PK widened to (server_id, pkg_source, package_name, arch); `server_patch_inventory`) + lockfile | db | 2 | — |
| 0.2 | Poller: persist sysDescr; walk `entPhysicalSoftwareRev`; write `os_family` from profile; stamp `identity_updated_at` on successful system-info polls | poller | 3 | 0.1 |
| 0.3 | Chassis promotion job (device_entities → devices, precedence rules) + template identity-key writeback (PAN/F5/FGT — the sole PAN fix; its extract rule can never fire) | api | 4 | 0.1 |
| 0.4 | Profile packs: `os_family` on all builtin profiles; F5/ASA extract rules only where sysDescr carries versions (verify live); extend `children` mappings with os_version_key/serial_key for FortiAP + Aruba APs | content | 3 | — |
| 0.5 | Agent (ZenPlus_Agent repo): `patch_inventory_v1` capability — Get-HotFix, CurrentBuild+UBR, ARP ProductCode/arch/source; server ingest branch (prune-on-snapshot) | agent+api | 5 | 0.1 |
| 0.6 | zentryc feedbuilder skeleton: repo, channel/manifest/signing (`zentryc-feed` keypair), KEV + EPSS + endoflife.date + cvelistV5 ingest, slice filter, validation gate, static publish | server | 8 | — |
| 0.7 | Django: `vulnfeed/manifest|report` endpoints + static tree + entitlement key on subscriptions | server | 3 | 0.6 |

**Exit:** live appliance identity coverage targets hit (CV-F12 acceptance); a signed `network-server` snapshot with KEV/EPSS/EOL + CVE definitions publishes daily.

### CV-E1 — MVP: feed sync + device matching + core UI (M)

| # | Task | Area | Est (d) | Depends |
|---|---|---|---|---|
| 1.1 | `migrate-081-compliance.sql` (module tables, permission backfill, GRANTs) + `migrate-082-compliance-alert-metrics.sql` (base = migrate-076's list, doc 03 §7) | db | 3 | — |
| 1.2 | `compliance_feed.py` loop: manifest/verify/download/load/offset + `.zvb` sideload + feed_status in checkin inventory | api | 6 | 0.6, 1.1 |
| 1.3 | Version comparators (`cisco_ios`, `pan`, `junos`, `semverish`, `win_build`, `dpkg`, `rpm`, `apk`) + golden-fixture test corpus | api | 5 | — |
| 1.4 | `compliance_match.py`: device PSIRT matcher (Cisco canonicalization both sides) + CPE fallback + EOL matcher; state machine incl. identity-staleness gate, first-evaluation alert suppression, asset-cleanup sweeper; comparator batches via `asyncio.to_thread`; incremental scheduling | api | 9 | 1.2, 1.3 |
| 1.5 | `compliance_score.py` + SLA stamping + daily snapshots | api | 3 | 1.4 |
| 1.6 | `compliance_alerts.py` + phrasing + rule metrics wiring | api | 3 | 1.4 |
| 1.7 | Router `/api/v1/compliance/*` (summary, vulns, findings, assets, feed, settings) + permissions + audit | api | 6 | 1.4 |
| 1.8 | zentryc: Fortinet CVRF + PAN API + cvelistV5-CNA (Juniper/Ubiquiti) + Cisco openVuln per-version cache ingestors | server | 8 | 0.6 |
| 1.9 | UI: ComplianceLayout, Overview, Vulnerabilities (+detail), Assets, Settings (feed card); nav+routes+smoke; extract page-local `TablePanel`/`MetricChartCard` from ServerDetailPage into a shared module | ui | 10 | 1.7 |
| 1.10 | Device Security tab + firmware slot; triage workflow UI (single+bulk with comments) | ui | 5 | 1.9 |

**Exit:** an operator sees real, vendor-advisory-grade CVE findings on the live Cisco/Forti/PAN fleet, triages them, gets channel alerts for critical/KEV, and the feed self-reports freshness fleet-wide.

### CV-E2 — Servers + software + air-gap (M)

| # | Task | Area | Est (d) | Depends |
|---|---|---|---|---|
| 2.1 | zentryc: MSRC CVRF pipeline (products, FixedBuild, KB supersedence) | server | 6 | 0.6 |
| 2.2 | zentryc: Ubuntu/Debian/RHEL/Alpine tracker ingest + distro rule compile | server | 6 | 0.6 |
| 2.3 | zentryc: alias dictionary seeding (winget manifests) + backlog loop over the report endpoint's `unmatched_software`/`observed_products` telemetry (doc 06 §4) | server | 5 | 0.6 |
| 2.4 | Matchers: MSRC build/KB, distro, alias, CPE-for-apps | api | 8 | 2.1–2.3 |
| 2.5 | Software page + server Compliance-tab extension + unmatched view | ui | 5 | 2.4 |
| 2.6 | Air-gap: portal pre-authorized bundle URL + dashboard upload UX polish | server+ui | 3 | 1.2 |
| 2.7 | Linux agent GA (packages incl. source-package fields) | agent | 8 | 0.5 |

### CV-E3 — Remediation, EOL board, reports, SLA polish (M)

Remediation computation + `/compliance/patches` page (3.1, 6 d) · EOL board + approaching-milestone logic (3.2, 4 d) · report sections ×5 (3.3, 6 d) · SLA policy editor + aging widgets (3.4, 3 d) · severity×age heatmap + trend charts (3.5, 3 d) · Cisco EoX entitlement integration if secured (3.6, 3 d) · delta publishing on the feed (3.7, 4 d).

### CV-E4 — Second pillar & intelligence (L, own specs when scheduled)

Config-compliance policy packs (CV-F14) · agentless Windows/SSH software collection · active verification probes (Nuclei-style, signed content) · AI copilot grounding ("what should I patch first?") · MSP/multi-tenant report branding.

---

## 2. Release & rollout discipline

- Each phase ships as normal OTA releases; migration + code together; `main` fast-forwards; migrations linted + lockfiled.
- **Dark-ship:** module code can ship before the feed goes GA — nav is gated on **permission only**; unentitled appliances see the module with the activation call-to-action (doc 03 §2.3); feed tables empty at install is the designed state.
- zentryc feed goes live before CV-E1's appliance release (appliances with the module but no feed show the sync CTA, nothing breaks).
- Companion fixes riding along (small, high value): NCM endpoints adopt `require_permission`; `run-scheduled` gets auth; note advisory-lock key collisions to the team.
- KB articles at `zentryc.com/kb/zenplus/compliance` land with CV-E1 (page-per-tab, i-icon links) — the UDT KB publishing runbook applies.

## 3. Test plan

Unit/fixture level:

| ID | Type | Case | Expected |
|---|---|---|---|
| T1 | unit | Comparator matrix per scheme (incl. `15.2(4)E8` vs `15.2(4)E10`, `1:1.2-3ubuntu1` epochs, `10.2.9-h1`, `21.4R3-S5.4`, `10.0.20348.2340`) | pinned orderings, cross-train comparisons refused |
| T2 | unit | Score formula components + caps + confidence multipliers | matches doc 03 §5 exactly |
| T3 | unit | Manifest verification (bad sig / stale / future / wrong major / sha mismatch) | each refused with distinct error |
| T4 | unit | Slice loader idempotency (same delta twice, out-of-order refusal) | single application, offset monotonic |
| T5 | integration | Golden fleet fixture (12 devices, 3 servers, pinned feed snapshot) → findings | exact expected finding set incl. tiers |
| T6 | integration | Ubuntu backport FP case | not flagged |
| T7 | integration | Version upgrade → auto-fix; downgrade → resurface | state transitions + events + single alert each |
| T8 | integration | Bulk triage 500 findings w/ comment | < 2 s, one audit entry per finding batch, history correct |
| T9 | integration | Feed re-import, zero inventory change | zero new alerts, zero event rows |
| T10 | integration | Remediation fixed-point (candidate version itself vulnerable) | next clearing version chosen |
| T11 | e2e | Fresh install → register → first sync → Overview populated | ≤ 1 sync cycle, all widgets live |
| T12 | e2e | Air-gap: unregistered appliance + `.zvb` upload | full function, no egress |
| T13 | security | Viewer role: read-only enforced server-side on every mutating endpoint | 403s |
| T14 | security | Bundle upload with tampered content by admin | refused; audit log entry |
| T15 | security | No credential material in any compliance response (incl. asset endpoints) | verified by response-schema scan |
| T16 | perf | 10k devices / 500k findings: list endpoints | p95 < 400 ms |
| T17 | perf | Full nightly re-match at 10k assets | < 15 min, bounded batches, no request-path impact; **agent-ingest latency unaffected while matching runs** (comparators in to_thread) |
| T18 | regression | migrate-080/081/082 on N-1 → N OTA and on fresh install | schema gate green both paths |
| T19 | regression | Alert-metric constraint supersede | every pre-existing metric still accepted |
| T20 | manual/UAT | Triage workflow with a real operator on the live fleet | states/comments/exports usable without docs |
| T21 | integration | Delete a device/server with open findings | cleanup sweeper removes its findings/meta/remediations; per-CVE asset counts and KPIs drop |
| T22 | integration | First-ever evaluation of a fleet (module enablement) | findings created, zero channel notifications, digest available |
| T23 | integration | Snapshot reload with zero content change | zero finding churn, zero events, zero alerts |

Cross-cutting (house test plan): RBAC on every endpoint, migrations idempotent on OTA, perf budgets, secret safety, upgrade N-1 → N. Live integration tests target a running uvicorn on :8001 (project convention).

## 4. Risks & mitigations

| Risk | Mitigation |
|---|---|
| False positives erode trust (the SolarWinds failure mode) | Vendor-advisory-first matching; confidence tiers with D/E hidden by default; evidence on every finding; `not_applicable` triage with audit; golden-fixture regression gate on the feed pipeline |
| NVD/upstream volatility (2024–26 track record) | Multi-source pipeline with per-source failure domains; cvelistV5 as primary; central curation absorbs breakage — appliances only see the validated slice |
| Bad feed publish bricks evaluations fleet-wide | Validation gate; >5%-delta forces snapshot; appliances keep last-good on any verify/load failure; un-publish kill switch |
| Cisco openVuln ToS for re-serving | Legal review before GA; fallback: serve derived match rules for fleet-observed versions only; worst case Cisco drops to CNA-record matching (Tier B) |
| Agent repo dependency (KB inventory, Linux GA) | Windows build-number path works with data already flowing (`kernel_or_build`); KB fallback and Linux path degrade gracefully — feature-gated by capability advertisement |
| zentryc host fragility (hand-deployed, no git) | Feedbuilder is a separate, git-managed deployable; appliance-facing Django delta is 3 endpoints + static files |
| Scale (500k findings) | Partial indexes, state-filtered queries, bounded batch loops; ClickHouse escape hatch documented but not needed for v1 |
| Scope creep into config-compliance | CV-F14 explicitly outlined-only; own spec when scheduled |

## 5. KPIs to track post-GA

Fleet feed freshness (p95 age) · unmatched-inventory % (dictionary backlog burn-down) · finding FP rate (share of findings triaged `not_applicable`) · median time-to-fix by severity · % appliances with module enabled · support tickets per 100 appliances mentioning compliance.
