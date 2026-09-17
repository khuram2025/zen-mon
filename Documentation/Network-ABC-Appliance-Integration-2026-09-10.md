# Network A–C appliance integration assessment and delivery plan

> **Status update:** The dependency gate was subsequently resolved and 1.23.11 was installed on the development appliance. See the [development rollout report and next-phase plan](Network-ABC-Development-Rollout-2026-09-10.md). The candidate identity and deployment status below describe the earlier integration milestone.

**Assessment:** A–C passed development database integration and is packaged as a signed, unpublished 1.23.11 candidate for review. Development-canary installation remains gated on dependency resolution and rollout checks below. It is not installed and does not establish hardware interoperability or full SolarWinds NPM parity.

This continues the [initial development report](Network-ABC-Development-Validation-2026-09-10.md). Missing live SNMP devices were explicitly accommodated through synthetic timestamped data and loopback protocol fixtures. Empty development telemetry is not classified as a missing feature.

## Appliance and candidate identity

| Item | Verified state |
|---|---|
| Appliance | `192.168.8.221` |
| Installed version | 1.23.10; commit `b4256ff0f0646db137ad2250f8d441b11464e7ee` |
| Installed source | Clean working tree; 588 inventoried application files |
| Running poller | Executable checksum matches the installed binary |
| Candidate | 1.23.11; commit `06f823f0e07d76bed1b740e67dd11758f68051f3` |
| Candidate branch | `codex/network-abc-integration` |
| Payload | Backend, dashboard, poller, NetFlow collector, remote sensor, migrations 115/116 and existing Windows agent installer 1.12.4 |
| Publication/deployment | Neither performed; installed API and poller remain active on 1.23.10 |

The application inventory has 10 added files, 26 changed files and no missing files. Its defined scope excludes tests, dependencies and configuration; the package verifier separately checked all 59 changed payload files against committed Git blob bytes. Unrelated local `ZenPlus_Agent` changes are excluded.

Package: `output/network-appliance-integration/update-1.23.11.zup` — 119,221,004 bytes (113.7 MiB).

```text
SHA-256: 4db5dfd7a6748075154ec1b0092c1fea290dbf6b7bc7fa86cb92919062ebdce2
```

The approved signing key matched the appliance's public key. Offline verification confirmed a valid signature, 852 checksums, 59 authoritative changed files, the bundled MSI and zero forbidden files. The signed upgrade prerequisite is 1.23.9. No private key or appliance credential was included in source or release artifacts.

## Confirmed defects fixed during integration

| Finding | Correction and evidence |
|---|---|
| Dependency recovery SQL interpreted JSON `false` as a SQLAlchemy bind parameter, aborting evaluation | Use PostgreSQL `jsonb_build_object`; real worker trigger/recovery and parent outage/recovery pass |
| Syslog maintenance suppression had the same problem with JSON `true` | Use `jsonb_build_object`; real persistence and suppression pass |
| Scheduled maintenance did not consistently suppress metric and syslog alerts | Both paths use the shared maintenance check; device/group/tag/global scopes pass |
| Manual maintenance could still produce a trap alert | Shared check recognizes manual state; all three event/metric paths pass |
| Legacy regression smoke tests could reach the installed API despite isolated database settings | Validation runner explicitly redirects those API tests to an unavailable loopback endpoint |
| Existing favorite-removal smoke test could choose an operator's starred link | Select the existing unstarred-link fixture; change reviewed and live mutation test skipped by the guarded runner |

These are implementation and validation defects, independent of equipment availability.

## Acceptance evidence

PostgreSQL 16 ran in a separate cluster using a private Unix socket and a schema-only copy of the installed database. ClickHouse 24.10 ran in a separate container exposed on host loopback. No application data was copied into these fixtures. Migrations 115 and 116 were applied repeatedly. Real foreign keys, transactions, advisory locks and ClickHouse queries were exercised.

| Check | Result |
|---|---|
| New PostgreSQL/ClickHouse integration suite | **17 passed, 0 failed, 0 skipped** |
| Guarded Linux Python regression | **772 passed, 0 failed, 42 skipped** |
| Linux Go: SNMP, store, pinger, sensor spool, sensor command | Passed, including loopback trap wire tests |
| Real PostgreSQL Go interface test | Passed; simultaneous upserts retain policy after index swap |
| Normal release build | Passed migration lint, deterministic dashboard dependency install, production dependency audit, Vite bundle and three Linux binary builds |
| Signed package verification | Passed; source bytes and transferred package digest match |
| Temporary fixture cleanup | PostgreSQL stopped, test ClickHouse container/volumes removed, remote temporary workspace removed |

The 17 new cases cover compound AND/OR conditions, trigger holds, reset thresholds, missing-data preservation and recovery; independent template components; concurrent evaluators; syslog replay/cooldown/scoping; concurrent discovery and policy preservation; invalid/failed imports; IPv6 trap storage; trap replay and dependencies; parent recovery; MIB upload/child-process compilation/symbol resolution/stale-index rejection; template round-trips; scoped API reads and retention SQL; and five maintenance modes across metrics, syslog and traps.

The scoped API test uses ASGI with a fixture authentication identity and real SQL. It is not an SSO test. Notifications use a local function sink. The retention test verifies old/current row boundaries, not a long-running retention service.

The guarded regression's 42 skips are recorded individually in `results.json`, including legacy live-API tests and a transport test needing a separate TLS helper. Earlier Windows password-length, POSIX permission and RADIUS failures passed on Linux. Sensor command tests also passed on Linux.

**Legacy smoke-test execution:** an earlier broader run used the repository's default installed-API address and finished with 810 passed and 4 skipped. This mixes staged unit tests with smoke tests of installed 1.23.10 and is not isolated candidate evidence. Its temporary servers and saved views were confirmed absent afterward. Read-only favorite-history inspection found six test insertions, six deletions and no older favorites deleted during those transactions. Audit/login/test telemetry may remain under normal retention. Installed code and schemas were not upgraded; `network_events` was confirmed absent from the installed database. The guarded runner prevents recurrence.

## Remaining limits

- Vendor SNMPv2c/v3 polling, contexts, IPv6, traps, reboots and high-speed counter accuracy require representative equipment or validated vendor simulators.
- Existing TypeScript debt remains: the initial comparison recorded 539 diagnostics versus 541 on baseline, with no new normalized diagnostics. The established builder uses Vite and passed; this is not a clean TypeScript result.
- Python checks used the appliance's existing Linux environment plus an isolated installation of `pysmi==2.0.0`. They are not a clean requirements-install test. `passlib[bcrypt]==1.7.4` leaves bcrypt unpinned; the previously observed bcrypt 5 incompatibility must be resolved or excluded by a verified dependency constraint before installing the candidate. A passing existing-environment test does not close that release gate.
- Sustained polling/event rates, query cost, queue loss and restart durability are not load-certified.
- Syslog is bounded UDP with best-effort processing. Cleanup currently runs when events arrive, at most hourly. Idle cleanup scheduling, TCP/TLS and durable spooling are not included.
- External SMTP/SMS/webhook delivery is unverified. Dispatcher success is not proof of receipt.
- SNMPv3 informs, full USM timeliness/replay certification, unified historical interface identity after reindexing, and wider topology/wireless/cloud/forecasting parity remain separate work.
- Initial UI fixtures passed; post-upgrade browser checks against the installed candidate remain a canary gate.

## Next delivery plan

| Step / owner | Work and acceptance gate |
|---|---|
| Review and dependency gate — development/reviewer | Review the two candidate commits and evidence; verify a clean requirements install and resolve the passlib/bcrypt compatibility constraint. Rebuild/reverify if dependencies change. Reconcile with protected-main workflow and preserve unrelated agent work. |
| Development canary — release/SRE | Back up the appliance, check free space/updater prerequisites, install through the normal updater. Verify version, schema ledger, service health, loaded binary checksums and rollback readiness. |
| Synthetic canary — QA | Repeat discovery, swap interfaces, replay counter resets, alternate tunnel states, exercise compound alerts/maintenance/dependencies and replay event IDs. Require one inventory identity, preserved policy, correct unknown/recovery behavior and no duplicate incidents. Inspect charts and event pages. |
| Optional syslog — release/network engineering | Install the supplied service template on loopback UDP/1514 first. Configure explicit fixture CIDRs before device-facing ingress; verify source filtering, storage, scoping and retention. |
| Hardware certification — network engineering | Execute the actual vendor/model/firmware/security matrix and relevant 1/10/100-Gbit/s traffic references. Record rates, reboot/reindex behavior and trap interoperability. |
| Wider release — release owner | Resolve or disposition TypeScript debt and review load, durability and external-delivery results. Publish through the protected-main workflow with staged rollout after canary acceptance. |

Rollback: stop the optional syslog receiver and disable its rules; restore the preceding application/dashboard/binary package through the normal rollback process. Retain additive schema and event data. Do not rewrite shipped migration checksums.

## Reproduction and artifacts

Committed runner: `scripts/validate-network-integration.py`. Tests: `server/tests/integration/test_network_contracts.py` and `poller/internal/store/interface_postgres_integration_test.go`.

```text
python scripts/validate-network-integration.py --fixture-root /tmp/zenplus-network-integration-<run> --prepare-databases
python scripts/validate-network-integration.py --fixture-root /tmp/zenplus-network-integration-<run> -- tests/integration/test_network_contracts.py -q
```

Prerequisites: staged source under the fixture root; private PostgreSQL `pgsocket` on port 15432 with user `network_fixture` and database `zenplus`; loopback ClickHouse HTTP/18123 with the dedicated username; private `clickhouse-fixture.env`; application dependencies and `pysmi==2.0.0`. Provisioning is separate from the test runner. Go also requires `ZENPLUS_NETWORK_INTEGRATION=1` and `ZENPLUS_NETWORK_PG_SOCKET` pointing to the fixture socket.

Evidence under `output/network-appliance-integration/` includes the signed package, metadata, Git bundle, committed-source verification archive, source/runtime identities, JUnit results, Go logs, build/verification logs, cleanup checks and `results.json`. The candidate worktree remains at `tmp/network-release-candidate`. The shared working tree and unrelated user changes are preserved.
