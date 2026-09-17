# Network monitoring development rollout and next-phase plan

> **15 September update:** D1/D2 receiver and simulated-device work is now installed as 1.23.12. See the [receiver validation report](Network-Receiver-Validation-2026-09-15.md), including a package-permission issue discovered and repaired under the service identity. This report records the earlier 1.23.11 milestone.

**Assessment: A–C is installed and development-validated on 192.168.8.221, version 1.23.11.** The clean dependency gate is closed. This is an unpublished development release, not a declaration of full SolarWinds NPM parity or hardware certification.

The absence of real network equipment is explicitly accommodated. Empty SNMP/interface charts and an empty discovery inventory are not evidence of missing features. Development tests establish software behavior; vendor interoperability, sustained load and external notification receipt remain separate acceptance gates.

This report supersedes the deployment/dependency status in the [earlier integration assessment](Network-ABC-Appliance-Integration-2026-09-10.md). That report retains the detailed A–C integration findings and earlier Go evidence.

## Delivered result

| Workstream | Installed behavior and evidence | Remaining boundary |
|---|---|---|
| A — Development validation | Reproducible isolated PostgreSQL/ClickHouse tests, synthetic time-series and event cases, guarded regression runner, source/package/runtime evidence | No real-device or sustained-load certification |
| B — Discovery and polling contracts | Validated discovery/import contracts; interface policy preservation; concurrent import/upsert checks; polling and counter handling; MIB compilation and template round-trips | Vendor SNMPv3/context/IPv6/reboot coverage and historical interface identity across reindexing require further work |
| C — Alerts and network events | Compound condition/hold/reset behavior; consistent maintenance/dependency handling; trap identity and replay tests; syslog persistence, scoping, cooldown, UI and alert controls | Persistent syslog receiver remains disabled; UDP durability, TCP/TLS and external delivery receipt are not established |
| Release dependency gate | Explicit `bcrypt==4.3.0` pin with passlib 1.7.4; clean Linux installation; compatibility with legacy bcrypt and bcrypt-sha256 v1/v2 hashes and long Unicode passwords | Complete transitive dependency lock remains a future reproducibility improvement; the resolved clean environment is recorded |

The bcrypt pin avoids the bcrypt 5 behavior that breaks passlib's backend self-test while preserving the existing password formats. The upstream project documents its change to reject passwords longer than 72 bytes: [bcrypt release history](https://pypi.org/project/bcrypt/). Application passwords continue to use bcrypt-sha256.

## Verification results

| Check | Outcome |
|---|---|
| Fresh Linux requirements installation | Passed; `pip check` reports no broken requirements |
| Regression suite in the fresh environment | **776 passed, 43 skipped, 0 failed** |
| Real PostgreSQL/ClickHouse synthetic integration suite | **17 passed, 0 skipped, 0 failed** |
| Password upgrade compatibility | Legacy bcrypt, bcrypt-sha256 v1/v2, incorrect passwords and long Unicode passwords passed |
| Signed release build | Passed; dashboard bundle, Linux binaries, migrations and artifact verification completed |
| Package verification | Valid signature; **853 checksums**, **60 changed source files** checked against committed blobs; zero forbidden files |
| Installed source comparison | **59 of 59 source files matched**; `.version` is separately checked because the updater adds its installation timestamp |
| Running binaries | Poller and NetFlow process executable hashes match the signed package; staged sensor binary also matches |
| Installed service health | API, poller, NetFlow collector, nginx and updater timer active; health endpoint reports API/PostgreSQL/ClickHouse/Redis/SNMP encryption OK |
| Database convergence | PostgreSQL and ClickHouse schema gates passed; no pending migrations reported |
| Live UI | Existing account sign-in succeeded; version 1.23.11 displayed; discovery and alert pages loaded; syslog severity/manual-resolution controls displayed |
| Live synthetic event | Installed ingestion function persisted one event; replay was deduplicated; the live API-backed UI displayed it; the exact test row was deleted afterward |

The 43 regression skips include the opt-in integration module and legacy tests requiring a live API or external helpers. The integration module was then run separately against disposable databases. The two test totals describe separate runs. Warnings remain; neither run failed.

The live event smoke used the installed ingestion function and database. It did **not** send a datagram through an enabled production syslog listener or contact a real device. Event alert dispatch tests use a local sink. No external notification was sent by these validation fixtures.

Existing TypeScript debt remains: the preceding baseline comparison recorded 539 diagnostics against 541 on baseline, with no new normalized diagnostics. The established Vite release build passed; this is not a clean TypeScript check.

## Release identity

| Item | Value |
|---|---|
| Development appliance | `https://192.168.8.221/` |
| Installed transition | **1.23.10 → 1.23.11**, 10 September 2026 |
| Candidate commit | `74552c0eebce25d5b3a93f57fa4a2218e25a8d9a` |
| Candidate branch | `codex/network-abc-integration` |
| Package SHA-256 | `eb7737859c63cbe95890115a465e0ebf2d402bb96fbc16a83f9610586082776a` |
| Public release / other appliances | Not published or deployed |
| Bundled Windows agent | Existing 1.12.4 installer; unrelated local 1.12.5 work excluded |

The installed repository's Git HEAD remains the pre-OTA checkout baseline, `b4256ff0f0646db137ad2250f8d441b11464e7ee`. The updater replaces managed files without creating a Git commit on the appliance. The signed package and installed file/process checksums establish the running release identity.

## Installation and recovery evidence

The local installation used the installed updater's signature/checksum/version checks, lock, signed manifest executor, backup/rollback handlers, migration gate and local update history. It did not publish the package to the central update server.

The first attempt failed because my local installation wrapper placed its extracted payload under a managed code directory. Code reconciliation removed payload files before a later hook could read them. The updater restored code and PostgreSQL, and the wrapper restored the Python environment. Version 1.23.10, the original poller checksum, active services and HTTP 200 were verified before retrying. This was a rollout-wrapper mistake, not an A–C feature failure.

The corrected wrapper staged under `/tmp/zenplus-updates/`, outside managed code. **All 29 signed installation steps passed**, followed by the full schema gate and service checks. The updater stamped 1.23.11 only afterward. The updater timer was restored to its prior active state.

Recovery material remains **only on the appliance**, protected by root permissions:

- Successful-attempt baseline: `/opt/zenplus/updater/backups/network-dev-20260910-retry/`.
- Earlier attempt and recovery evidence: `/opt/zenplus/updater/backups/network-dev-20260910/`.
- Each contains updater code/database backups and an additional Python-environment archive. The successful-attempt directory also retains configuration recovery material, schema results and installation logs.

Recovery is not a complete machine snapshot. Package-manager and external system configuration changes require separate assessment during rollback; Python restoration from an archive is an overlay. ClickHouse changes in this release are additive and its full data was not backed up. A full production rollback rehearsal remains a later gate. Do not overwrite shipped migration checksums or restore an older database dump without considering data recorded since the backup.

## Next-phase plan

| Priority / owner | Work | Acceptance evidence |
|---|---|---|
| **D1 — Backend/SRE** | Harden the event receiver: controlled listener configuration, periodic retention independent of incoming traffic, queue/drop visibility and bounded shutdown/restart behavior | Loopback simulator proves ingest, filtering, alert creation, acknowledgement/resolution and restart behavior; counters make any loss observable; no exposure beyond the chosen listen scope |
| **D2 — QA/Backend** | Build a repeatable simulated-device campaign for discovery → import → polling → alert → recovery, including ifIndex changes and counter resets | Repeated runs preserve monitoring policy and produce the expected history and incident lifecycle; failures retain evidence and clean up only tagged fixtures |
| **D3 — SRE/QA** | Establish polling/event performance budgets and a controlled soak test | Report device/interface/event rates, poll lateness, CPU/RAM, database growth, query latency, queue depth and drops against agreed deployment targets |
| **D4 — Network/QA** | Run the representative vendor lab when devices or validated vendor simulators are available | SNMPv2c/v3 authentication/privacy/context, IPv6, traps, reboot/counter behavior and high-speed interfaces verified; development telemetry absence does not fail this gate prematurely |
| **D5 — Release/QA** | Verify notification receipt with an authorized test destination; rehearse complete rollback and review the protected-main release | Evidence of receipt and recovery, reviewed package provenance, then a staged production rollout |
| **Subsequent parity work — Product/Engineering** | Revisit topology/dependency depth, wireless/controller coverage, path diagnostics, forecasting/reporting and remaining trap/SNMPv3 capabilities against the original NPM comparison | Feature-specific demonstrations and acceptance tests; adjacent SolarWinds modules kept distinct from NPM scope |

**Recommended immediate step: D1, followed by D2.** This closes receiver-operability and repeatable end-to-end validation gaps while the environment remains development-only. No additional deployment or central publication is implied by this plan.

## Reviewable artifacts

Evidence lives in `output/network-development-rollout/`: signed package and metadata, candidate Git bundle/source list, authoritative-source archive, build and verification logs, clean dependency freeze, JUnit results, schema verdict, installation/rollback results, runtime attestation and live UI captures.

- [Runtime attestation](../output/network-development-rollout/rollout-attestation.json)
- [Installation result](../output/network-development-rollout/install-result.json)
- [Package verification](../output/network-development-rollout/candidate-verification.log)
- [Live syslog UI](../output/network-development-rollout/live-syslog-ui.png)
- [Live alert wizard](../output/network-development-rollout/live-alert-wizard.png)

Unrelated work in the shared workspace is preserved. The isolated candidate worktree remains available for review. Disposable validation databases and remote build staging are removed after evidence collection; appliance backups are retained.
