# 04 — Feature Specification

*Status: Design proposal · 2026-08-18. Each feature is a binding contract: priority, behavior, data, API, acceptance. Architecture detail in doc 03, UI detail in doc 05, phases in doc 07. Priorities: **P0** = module doesn't ship without it · **P1** = first GA release · **P2** = fast-follow.*

---

## CV-F1 — Feed sync from zentryc.com (P0)

**Behavior:** `compliance_feed_loop` polls the manifest every 6 h ± 30 min (ETag), verifies Ed25519 signature + 7-day freshness + schema major, downloads snapshot/deltas resumably, loads transactionally into feed tables, advances `vuln_feed_state`, reports applied offset upstream, and marks changed CVEs for re-match. Failures keep last-good data and set `last_error`.
**Data:** `vuln_definitions`, `vuln_affects`, `eol_definitions`, `compliance_oid_dictionary`, `compliance_product_aliases`, `vuln_feed_state`.
**API:** `GET /compliance/feed/status` [view] · `POST /compliance/feed/sync-now` [manage, audited].
**Acceptance:**
- [ ] Tampered manifest/signature, stale `built`, future `built`, wrong schema major, sha mismatch → load refused, last-good data intact, error surfaced in status + `compliance_feed_stale` path unaffected.
- [ ] Delta chain applies exactly once (offset idempotency); out-of-window client degrades to snapshot automatically. *(Fixture/unit-verified in CV-E1 — the live feed publishes snapshot-only until CV-E3 task 3.7; re-accept against the live feed then.)*
- [ ] Snapshot reload with zero content change → zero finding churn, zero events, zero alerts (loads are diff-based; `op: delete` tombstones, never deletes).
- [ ] Kill the process mid-load → next run recovers cleanly (transactional load; offset only advances with data).
- [ ] Unentitled appliance: sync refuses with a clear status message; UI shows activation CTA.
- [ ] `share_telemetry` off → report payload carries status fields only (no product tuples / software names), and the Cisco path visibly degrades to Tier B.

## CV-F2 — Air-gapped bundle import (P1)

**Behavior:** manage-gated upload of a `.zvb` bundle; identical verification path as network sync + monotonic offset check; works on unregistered appliances. Pre-authorized download URL lives on the customer's zentryc subscription page.
**API:** `POST /compliance/feed/upload-bundle` [manage, audited].
**Acceptance:** valid bundle loads and matches end-to-end with no egress; rollback bundle (older offset) refused with explicit message; malformed tar rejected safely (path-traversal guard reused).

## CV-F3 — Device firmware matching (P0)

**Behavior:** per-vendor matchers evaluate `(vendor, os_family, os_version, model/platform)` against `vendor_os` rules — exact-version sets (Cisco) or per-train ranges with vendor-grammar comparators (FortiOS, PAN-OS incl. `-hN`, Junos, ArubaOS, RouterOS). Platform-scope confirmation required for Tier A; version-only match is Tier B. Config-dependent advisories carry `condition_note` into the finding. CPE fallback (Tier C/D) only where no vendor coverage exists.
**Depends on:** CV-F12 identity hardening (os_family, promoted versions).
**Acceptance:**
- [ ] Fixture fleet (real strings: `17.9.4a`, `15.2(4)E8`, `v7.6.7,build3704`, `11.1.10-h25`, `21.4R3-S5`) matches a pinned expected finding set exactly (golden-file test), including Cisco canonical-form equivalence (`17.06.04` ≡ `17.6.4`).
- [ ] A device with no version data produces zero device-OS findings and appears in a "not evaluable" count, never a false "clean".
- [ ] A device with stale identity (`identity_updated_at` > threshold, or down) is not re-evaluated: findings flagged `identity_stale`, device counted "not evaluable".
- [ ] Version upgrade on the device auto-closes findings (state → `fixed`) on the next match run.

## CV-F4 — Server OS + patch matching (P0 Windows, P1 Linux)

**Behavior:** Windows — map `(os_name, kernel_or_build)` to MSRC product; vulnerable ⇔ build < `fixed_build` (numeric 4-part); KB-presence fallback (Tier C) using `server_patch_inventory`; recommendation = newest cumulative KB. Linux — `(servers.distro_id/distro_release/distro_codename, source_package, version)` vs distro-feed ranges with dpkg/rpm/apk comparators (the normalized distro columns from migrate-080 are the key — never free-text `os_name`); distro feeds only (backport-aware); NVD never judges distro packages.
**Depends on:** agent `patch_inventory_v1` (KB list; UBR already flows in `kernel_or_build`); Linux agent GA for the Linux path.
**Acceptance:**
- [ ] Backport fixture: Ubuntu `openssl 3.0.2-0ubuntu1.15` with the USN fix applied → NOT flagged (the canonical FP test).
- [ ] Windows fixture: build below/above `fixed_build` flips the finding; installed superseding KB clears the KB-fallback finding.

## CV-F5 — Installed-software (third-party app) matching (P1)

**Behavior:** normalize ARP names (strip arch/locale/version tokens), look up `compliance_product_aliases` → canonical identity → bounded ranges (Tier B); evidence-scored CPE fuzzy fallback (Tier C/D, unbounded CPEs capped at D); unmatched rows recorded as Tier-E "unidentified" for the dictionary backlog (visible on the Software page).
**Acceptance:** alias-covered fixture apps (Chrome, 7-Zip, PuTTY, Java with `version_transform`) match with correct fixed versions; a deliberately ambiguous name produces at most a D finding, filtered by default; unmatched-% metric visible.

## CV-F6 — Findings lifecycle, triage & audit (P0)

**Behavior:** auto states `open/fixed/resurfaced` recomputed by the matcher; manual triage `none/confirmed/not_applicable/risk_accepted/remediation_planned` with comment (required for `risk_accepted`), optional expiry on `risk_accepted` (auto-revert + resurface past expiry), single + bulk, full append-only history (`vuln_finding_events`) with actor + timestamps; `not_applicable` resets only when the matched version changes. Exception log (asset, finding, justification, approver, expiry) is filterable and exportable.
**API:** `GET /compliance/findings` [view] · `POST /compliance/findings/bulk-triage` [triage, audited].
**Acceptance:** every transition appears in history with actor; bulk change of 500 findings < 2 s; triage survives feed re-imports and re-matches; RBAC: viewer can read, cannot triage.

## CV-F7 — Risk scoring & SLA (P0)

**Behavior:** FRS (0–100) and ARS (0–1000) per doc 03 §5, components stored and rendered ("Critical severity · Medium risk" dual display); SLA `due_date` from the active policy pack (KEV rules outrank band rules; dynamic re-derivation on KEV addition/exposure change); aging buckets; daily posture snapshot row.
**Acceptance:** score components in every API response multiply to the shown score; policy edit re-stamps due dates on open findings; KEV finding always shows the KEV badge + due date regardless of CVSS; the worked examples in doc 03 §5.1 are encoded as unit tests.

## CV-F8 — Remediation recommendations (P1)

**Behavior:** `vuln_remediations` computed per asset per doc 03 §4.3 (in-train first, fixed-point-checked, train-jump separated; newest cumulative KB; package fixed versions; `replace_eol` for EOL assets — EOL trains never recommended). Grouped view with "clears N CVEs / risk −X"; "Mark remediation planned" sets triage on all covered findings.
**Acceptance:** recommendation for the Cisco fixture equals the hand-computed lowest clearing in-train version; marking planned flips N findings' triage in one transaction with one audit entry.

## CV-F9 — EOL tracking (P1)

**Behavior:** EOL matcher joins asset `(vendor, product/os_family, cycle)` to `eol_definitions`; findings per milestone (`approaching` = within 90 d, `eoas`, `eol`, `extended`); EOL is a first-class asset attribute/filter; past-EOL assets get standing findings and `replace_eol` remediation.
**Acceptance:** date-driven fixtures produce/resolve milestone findings as dates cross; EOL badge/filters on Assets consistent with the EOL page.

## CV-F10 — Alerts (P0)

**Behavior:** rule-driven metrics `compliance_critical_open`, `compliance_kev_open`, `compliance_eol_reached` (asset-scoped) and `compliance_feed_stale` (appliance-scoped), raise/resolve deduped, dispatched through channels; phrased in `alert_phrasing.py`; fire on transitions only.
**Acceptance:** feed re-import with no state change produces zero alerts; **an asset's first-ever evaluation (and module enablement on an existing fleet) dispatches zero channel notifications** — digest only; new critical Tier-A finding alerts once and resolves when fixed; threshold/confidence gates honored; migrate-082 keeps every pre-existing metric working (base = migrate-076's list verbatim + `tpl\_%` clause + append, per doc 03 §7).

## CV-F11 — Compliance UI section (P0)

Doc 05 in full: Overview, Vulnerabilities (+detail), Assets, Remediation, EOL, Software, Settings; device Security tab + server Compliance-tab extension; KB links.
**Acceptance:** smoke:routes green; every widget number clicks through to the matching filtered list; three-state handling on every table; viewer/operator/manage affordances gated by `useCan`; dark + light themes clean.

## CV-F12 — Inventory & identity hardening (P0 — prerequisite work)

**Behavior:** migrate-080 columns (incl. `product_code`, normalized distro identity, PK widening); poller persists sysDescr + walks `entPhysicalSoftwareRev` + stamps `identity_updated_at`; chassis model/serial promotion job (precedence rules, operator values never overwritten); template identity-key writeback (PAN/F5/FGT versions → `devices.os_version` — the sole PAN fix, its extract rule can never fire); profile packs gain `os_family` (+ extract rules only where sysDescr carries versions); **vendor packs' `children` mappings gain `os_version_key`/`serial_key` for FortiAP/Aruba AP children** (else promoted APs stay un-evaluable); feed-updatable OID dictionary supplements hardcoded lists; agent `patch_inventory_v1` + software-inventory field extensions (arch, pkg_source, source_package, raw_name, product_code) + `/etc/os-release` identity.
**Acceptance:** on this appliance's live fleet (non-empty basis): model coverage 4/37 → ≥ 30/37, serial 0/37 → ≥ 25/37, os_version 17/37 → ≥ 33/37 after one poll cycle + promotion run, **counting via-controller children**; PAN devices show `11.1.10-h25` on `devices.os_version`; DeviceDetail firmware slot renders.

## CV-F13 — Report sections (P1)

`compliance_executive_summary`, `compliance_vuln_detail`, `compliance_sla`, `compliance_delta`, `compliance_eol` registered in the section-based report engine; schedulable/exportable like existing sections. Generated compliance reports are retained in the report archive ≥ 12 months (audit evidence norm).
**Acceptance:** each section renders with fixture data in HTML + PDF export paths; delta section correct across two known snapshots; archive retention enforced.

## CV-F14 — Config-compliance policies (P2 — the second "compliance" pillar)

**Behavior (outline only, own design doc when scheduled):** Wazuh-SCA-shaped YAML policy packs (CIS-derived) evaluated against latest `device_configs` (NCM) and server inventory; per-policy %-score per asset; framework tags (PCI/NIST/CIS) on rules → per-framework report. Shipped as feed content (signed), not code.
**Acceptance:** deferred to its own spec; the data model reserves nothing — policies get their own tables when built.

---

## Cross-cutting non-functionals (every feature)

- RBAC enforced server-side on every endpoint (`require_permission`), audit on every mutation.
- Migrations replay-safe, probeable, lockfile-registered; content never in migrations.
- List endpoints p95 < 400 ms at 10k devices / 500k findings (indexed filters + pagination).
- Matching never on the request path; loops advisory-locked, batch-bounded.
- No secrets in responses; feed/bundle verification cannot be bypassed by any role.
- Every finding explains itself: evidence + score components + source feed, one click away.
