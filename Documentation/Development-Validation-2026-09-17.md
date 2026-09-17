# Development validation and release blockers — 17 September 2026

Application commit: `0dfa55fa9fc99c1fd06831750c5c6d1e559484ea` on
`codex/rum-production`, continuing the handoff at `9e7fc6f`.

## Source synchronization

The destination's installed checkout was on an older `smo_app` commit with
substantial uncommitted changes. It was preserved. Development and validation
ran in a separate Linux checkout of the GitHub branch.

The initial comparison found 804 matching application files, 17 differing
files, and 17 missing files. The missing files included the new NCM services
and migrations 117/118. Appliance-local Device Tracker and service-failure
work was reconciled into this branch; newer concurrent service-detail changes
and their supporting components were included after fingerprint verification.
The final comparison against the accepted source snapshots found no additional
concurrent changes. Older installed NCM code did not replace the newer GitHub
implementation. Other remote branches were not merged implicitly.

Changes in this continuation:

- Preserve IP observation periods, reporter/interface evidence and stable
  primary addresses; expose evidence and history pagination in Device Tracker.
- Add migration 119 for the evidence table and history index already used by
  appliance-local work. Historical migration files/checksums remain unchanged.
- Keep Device Tracker in its own sidebar group.
- Preserve readable probe failure details and daily failed-check evidence.
- Fix missing imports and active-page typing issues; remove 18 superseded UI
  modules proven unreachable from `main.tsx` by TypeScript's dependency graph.
  The active router remains covered by 36 route smoke checks.
- Isolate the login quota test from an installed administration access policy.
- Move the IIS script test into the Windows-specific test file; preserve the
  test while restoring Linux agent core validation.
- Add independent server, dashboard, poller and Linux/Windows agent CI jobs.
  Installer creation remains in the existing signing-gated release workflow.
- Fix RUM retention configuration loading during the first ten minutes after
  boot, with regression coverage at multiple system uptimes.
- Use concrete file read/execute rights for the Windows dashboard ACL and
  retain the elevated native integration test that rejects writable ACLs.
- Fetch full Git history in server CI so historical migration integrity checks
  can inspect earlier commits.

## Executed validation

| Check | Result |
| --- | --- |
| Fresh Python requirements install and `pip check` | Passed, including bcrypt 4.3.0 compatibility pin |
| Dashboard `npm ci` | Passed; audit reported zero vulnerabilities |
| Full isolated Python suite | 829 passed, 45 skipped; skips are not acceptance evidence |
| Fresh GitHub server suite at the application commit above | 831 passed, 46 skipped; dependency and migration checks passed |
| NCM PostgreSQL/loopback SSH fixture suite | 51 passed; 117/118 applied twice |
| Network PostgreSQL/ClickHouse fixture suite | 22 passed |
| Complete migration lint | Passed through new migration 119 |
| Fresh PostgreSQL schema and lockfile-ordered migrations | Passed through 118, then 119; repeat status check reports no pending migrations |
| ClickHouse convergence | Complete locked set through 116; repeat probe has no pending, failed or unresolved migrations |
| UDT history/evidence PostgreSQL regression | Passed, including preserved periods and independent reporter evidence |
| Poller Go tests | All packages passed, including the database-backed UDT regression |
| Poller, NetFlow collector and sensor Linux builds | Passed; poller rebuilt after UDT reconciliation |
| Agent Linux core tests | All internal packages passed after platform-specific test correction |
| Agent Windows cross-build and native Windows CI | Passed; installer signing remains a separate gate |
| Strict dashboard `npm run build` | Passed; the handoff's 539 TypeScript diagnostics are resolved |
| Dashboard route, RUM, network, availability, failure and daily-probe contracts | Passed |
| Installed service-account dependency imports and `pip check` | Passed |
| Installed health endpoint | HTTP 200, status `ok` |
| NCM/syslog systemd template verification | Passed; existing installed API/poller units warn about `StartLimitIntervalSec` placement |

The existing large Vite chunk warning remains. Disposable PostgreSQL sockets
and a separate loopback ClickHouse container were used for integration tests.
Tests did not use the installed application's databases as fixtures, enroll
real NCM devices, change router configuration, or deliver real notifications.

All five development CI jobs passed on the application commit above:
server, dashboard, poller, Linux agent and Windows agent. See
[the completed development run](https://github.com/khuram2025/zen-mon/actions/runs/35223655927).
[PR #13](https://github.com/khuram2025/zen-mon/pull/13) is open for independent
review. Main protection now requires those checks and one independent approval.
The clean appliance-side review checkout matches the pushed application source;
comparison with the tested staging tree found only normalized line endings and
a trailing blank line. The disposable PostgreSQL fixtures were stopped after
validation. The installed application tree was not replaced.

## Release is not complete

The installed API, poller and NetFlow collector are running. The NCM worker and
timers are not installed, and the installed PostgreSQL ledger does not contain
117/118. The live deployment was not overwritten. Loopback login returned 403
under the appliance's administration policy; that policy was preserved.

The following gates remain:

1. Independent PR approval before a reviewed merge. Development CI is green;
   release signing configuration is still missing.
2. Production Windows signing configuration. The earlier Windows workflow
   failed specifically at `Require production signing configuration`; no
   signing secrets were configured at inspection. Existing Azure signing PR
   #11 is unreviewed and was not merged into this work.
3. Authorized OTA signing key and portal administrator credentials. The
   runbook's expected key/admin credential files were absent on this builder.
   Select a monotonically higher release version only after checking the
   authenticated portal catalog.
4. Named canary, NCM pilot devices, verified device SSH trust, protected key
   recovery and the operational acceptance described in the handoff. Broader
   NCM roadmap and real-device certification are not claimed complete.
5. Build from the exact reviewed `main` commit, verify its signed package,
   publish to canary, inspect package hashes/schema/services/login/features
   after installation, and complete a clean observation window before promotion.

No version tag, OTA package, portal upload, canary rollout or Windows installer
was published by this validation. Do not treat passing fixture tests as a
production or fleet acceptance result.

## Reproduction

Use an LF checkout and install the locked dependencies. Set `ZENPLUS_DIR` to
that checkout and `ZENPLUS_API=http://127.0.0.1:1` for isolated contract tests.
The new development workflow records the regular test/build commands.

Integration fixtures retain the contracts documented in
`Development-Appliance-Handoff-2026-09-17.md`: PostgreSQL 16 private sockets on
15432/15433, a separate ClickHouse fixture on loopback 18123, and disposable
source trees under `/tmp/zenplus-{network,ncm}-integration-*`. Use
`UDT_TEST_DSN` only for a disposable PostgreSQL fixture when running
`server/tests/test_udt_history.py` and the poller's UDT database regression.

Preserve the installed tree and backups until the reviewed OTA path succeeds.
Rollback should restore the preceding code/binaries and keep additive schema
and historical observation data; never remove history or rewrite old checksums.
