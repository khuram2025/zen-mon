# NCM implementation progress

Authorized 2026-09-16: implement the assessment priorities in order, test each set, and continue through the plan.

Baseline: `output/pdf/ZenPlus-NCM-Assessment-2026-09-10.md` (65 feature lines). Existing unrelated workspace edits must be preserved. A matching release label is not a deployed binary verification.

## Release gates

- [ ] P0: validated captures; unsupported device rejection; security-sensitive comparisons; NCM permissions and tag scope; protected scheduler; encrypted archive; reliable notification outbox; eligible/fresh coverage; pilot onboarding.
- [ ] P1 archive/recovery: durable jobs, startup configuration, baselines, retention, arbitrary/cross-device/raw diffs, search, restore validation and drills.
- [ ] P1 governed changes: immutable plans, approvals, templates, pre/postchecks, rollback, audit, compliance and reports.
- [ ] P2: certified vendor expansion, remote collectors, disaster recovery, integrations, lifecycle/vulnerability intelligence, firmware operations and assurance.
- [ ] P3: targeted ACL/policy analysis and demand-led legacy functions.

## Validation rules

Use synthetic unit and API tests, disposable database integration tests, dashboard build checks, and representative device lab tests. Production configuration writes, firmware upgrades, destructive fault injection and real outbound messages are not test fixtures. Record tested scope and unresolved deployment requirements without claiming full parity based only on implementation.

## Implemented in the workspace — 2026-09-16

These changes are implemented and tested in an isolated fixture. **They are not deployed to the production NCM service. P0 operational acceptance and the full assessment roadmap remain incomplete.**

| Priority | Delivered behavior | Evidence / limits |
|---|---|---|
| P0 | Reject unsupported drivers, empty/error/truncated output, unfinished pagers, missing terminators and invalid XML | Driver and transport tests. Physical model/OS certification remains open. |
| P0 | SSH peer verification using a managed known-hosts file | Actual loopback SSH captures of running/startup configuration; untrusted key rejected. Production device trust entries remain to be provisioned. |
| P0 | Preserve password, key and certificate changes in comparisons; redact display separately | Tests cover secrets, PEM, certificate chains, XML secrets and runtime metadata. Redaction is pattern based and requires additional vendor corpus review. |
| P0 | Enforce NCM permissions and tag scope on device content, fleet totals, profiles, comparisons and bulk operations | Real PostgreSQL and HTTP negative tests. Shared credential administration requires unrestricted scope. |
| P0 | Require authorization for HTTP scheduling; invoke scheduling locally through an unprivileged service | HTTP 401/403 checks; systemd syntax validation. |
| P0 | Encrypt new snapshots, verify integrity, migrate old plaintext archives in resumable batches | AES-GCM tampering, legacy migration and preserved content tests. The key remains the platform SNMP_ENC_KEY; coordinated key rotation/offsite recovery is not delivered. |
| P0 | Retain meaningful changes, pins, latest snapshot and the last validated recovery copy | Real database retention/deduplication tests. Baseline revisions remain protected. |
| P0 | Transactional change/failure events and notification outbox with retries and terminal failures | Provider rejection, retry, disabled-channel and terminal-failure tests; test sinks only. Supports SMTP, webhook, Slack, Teams, Discord and PagerDuty. “Delivered” means provider acceptance, not recipient receipt. |
| P0 | Measure fresh validated coverage for each required configuration type, within eligible scope | Imports/old success flags do not establish freshness; running-only evidence cannot satisfy running+startup coverage. |
| P1 foundation | Durable queued backups, four worker slots, leases, retry budget, progress and cancellation | Lease recovery, duplicates, failures and cancellation tested. Cancellation occurs between captures. Legacy synchronous config-fetch remains for compatibility. |
| P1 foundation | Minute scheduler with IANA timezone and due-run deduplication | Scoped scheduling and duplicate-schedule tests. Settings UI includes timezone. |
| P1 foundation | Explicit named baseline revisions, reason, actor and protected versions | Unvalidated imports cannot become baselines. Historical baseline versions survive retention and cannot be unpinned. This is not the future two-person change approval workflow. |
| P1 foundation | Running/startup selection, arbitrary history pair comparison, baseline comparison and cross-device comparison API | Running/startup loopback SSH tests and cross-device scope tests. Cross-device comparison is currently API only. |
| P1 foundation | Append-only application audit records and run/delivery/job history | Read, export, credentials, capture, settings, baseline and job audit paths. Database administrators retain database control; offsite immutable evidence remains open. |

Main implementation: `server/app/api/v1/ncm.py`, `server/app/services/ncm_*.py`, both NCM dashboard pages, migrations 117/118, NCM systemd units and release integration.

## Validation results

- **51 NCM tests passed** on the appliance's Linux runtime using a disposable PostgreSQL 16 cluster on a private Unix socket. No installed API or production database was used by these tests.
- **3 additional Linux security tests passed** in that fixture: private SNMP credential-file permissions and authenticated RADIUS reply handling. Combined final fixture run: **54 passed**.
- The NCM suite includes an actual local SSH server, supported running/startup collection, strict peer trust, cryptographic tampering, scope/role negative checks, migrations applied twice, retention, failed-capture preservation, protected baselines, job leases/retries and notification delivery failures.
- Broader Windows regression run: **820 passed, 45 skipped, 3 platform-specific failures**. Those three tests subsequently passed on Linux. The final three loopback SSH NCM tests were added after that broad run and passed separately on both platforms.
- Local targeted validation of NCM, migrations, password compatibility and applicable security cases: 86 passed before the final transport tests were added.
- Production dashboard bundle builds successfully. Project-wide TypeScript checking still reports 539 diagnostics outside the two NCM pages; no NCM page diagnostics were reported in that run. Existing large-bundle warning remains.
- Migration lint passes with both new migrations recorded in `scripts/migrations.lock`.
- NCM systemd unit verification passes. It reports existing `StartLimitIntervalSec` placement warnings in the installed API/poller units, outside these NCM units.
- No production router changes, firmware operations, restore commands or real outbound messages were performed.

Tests: `server/tests/test_ncm_assurance.py`, `server/tests/integration/test_ncm_contracts.py`. Fixture runner: `scripts/validate-ncm-integration.py`.

## Current appliance and outstanding input

A read-only inventory query on 2026-09-16 confirms **40 device records, zero NCM connection profiles and zero configuration versions**. Fifteen non-loopback records have network-device types and are candidates for review; this does not establish reachability, production eligibility or supported OS versions. The remaining inventory includes loopback labs, servers, printers and public DNS endpoints.

The user has been asked to identify the initial backup pilot and a disposable restore/change lab. Appliance administration credentials are available, but they do not establish network-device CLI access. Device CLI credentials should be entered into the approved private credential source or NCM profiles, not this report. No baseline or fresh-backup claim has been fabricated for the existing inventory.

## Next steps and acceptance gates

1. **Finish P0 operational acceptance.** Select reachable pilot devices and obtain their vendor/model/OS/context and CLI access. Verify SSH fingerprints out of band, configure `/etc/zenplus/known_hosts` for the service account, verify the encryption key and protected recovery copy, prepare an NCM release isolated from unrelated workspace edits, apply migrations 117/118, migrate/verify existing archives, start the worker/timers, and enroll the pilots. Verify real complete captures, denied privilege, interruption, unchanged runs, secret changes, stale/failure reporting and controlled notification delivery. Capture model/OS evidence before calling a driver certified.
2. **Finish P1 archive/recovery.** Add archive search and richer comparison controls, offsite manifests/export and coordinated key recovery, backup health reporting and stale notifications. Test bare-device recovery and timed restore drills on the selected lab. Confirm RPO/RTO and required artifacts beyond running/startup text.
3. **P1 governed changes.** Build immutable restore/change plans, separate propose/approve/execute permissions, approval expiry and self-approval denial, reviewed templates, pre-change backup and health checks, canary sequencing, postchecks, abort and tested OS-specific rollback. Gate execution on the lab results. No automated restore/change executor is delivered by this batch.
4. **P1 compliance and reporting.** Add versioned required/prohibited/block rules, applicability and unknown results, evidence, expiring exceptions, reviewed platform policy packs, remediation drafts routed through change approval and scheduled audit reports.
5. **P2 enterprise functions.** Certify additional drivers and context/artifact coverage; implement remote collectors and failure routing, offsite/appliance DR, normalized hardware/OS inventory, sourced CVE/EOL feeds and offline imports, ITSM lifecycle and controlled firmware repository/upgrades. Test site outage, key/database recovery and vendor upgrade/recovery scenarios.
6. **P3 specialized functions.** Prioritize parsed ACL/PAN policy analysis and legacy functions against actual installed-device demand after foundational operational gates pass.

The original assessment's remaining features are not marked complete merely because related schema, API or UI foundations now exist.
