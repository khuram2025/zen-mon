# NCCM development review — verification and implementation record

Reviewed document: `NCCM-development-review.md` (1 October 2026, appliance 10.11.40.31, release 1.23.19).
Verified against the 1.23.19–1.23.21 source (NCCM files are identical across those releases) and fixed on branch
`fix/nccm-development-review`.

## 1. Verdict on the findings

All 24 findings were reproduced in source. 23 are accurate as written; one (B01) is a real code defect but its
evidence points the wrong way.

| ID | Verdict | Notes |
|---|---|---|
| B01 | **Defect confirmed, evidence questionable** | The UI measured ages with the browser clock and had no time-sync health. However, the appliance's reported 10:04 UTC on 30 Sep matches an independently NTP-synchronised appliance (≈11:50 UTC shortly after). The reviewer's computer was most likely ~12 h fast, which also explains "13h ago" on fresh jobs. The appliance still reported `NTPSynchronized=no`, and that remains a real risk. |
| B02 | Confirmed | `enqueue` checked only enrollment, enabled state and profile. The scheduler and `/config-fetch` also bypassed readiness. |
| B03 | Confirmed, **worse than reported** | Besides `enabled=true`, every bulk save also overwrote schedule, retention and alert settings on every target with the first selected device's values. It also wrote audit rows for skipped devices. Bulk-assign re-enabled disabled devices too. |
| B04 | Confirmed | Trash icon → immediate DELETE; `ON DELETE SET NULL` silently unassigned devices. |
| B05 | Confirmed | No UI used `/ncm-runs`; runs had no job link, stage or category. |
| B06 | Confirmed | One active alert and one notification per failed attempt; nothing correlated or resolved them. `_FetchError` for disabled/unenrolled devices was also retried. |
| B07–B24 | Confirmed | See the fix table below. B18's mechanism: grid children lacked `min-w-0`, so the table's own scroller grew to the table width. |

### Additional defects found during verification (not in the review)
1. **HPE Comware / Huawei captures could never validate.** The format markers required `sysname` at column 0, but those platforms indent it (` sysname X`).
2. **FortiGate SSH hardening was silently dropped.** Netmiko's `FortinetSSH` replaces `disabled_algorithms` wholesale, discarding the SHA-1 `ssh-rsa` host-key restriction. The collector now merges both.
3. **SMS notification channels were accepted by the UI but always failed** (`sms` was missing from `SUPPORTED_CHANNELS`). SMS delivery now checks provider acceptance.
4. **Enrolled devices outside the automatic scope showed "Excluded"**, which hid their real failures (the review's out-of-scope Cisco case). They now show their operational state, marked "outside coverage scope".
5. **Enabling a daily/weekly schedule after its time fired immediately.** For example, "02:00" set at 12:47 ran at once. The schedule is now anchored so it starts at its next slot.
6. **A backward clock correction could stall interval schedules** until the future `last_scheduled_at`. This is now detected and audited.
7. **The Network & Security dashboard reported "backed up / all devices"** (row counts). It now reports fresh validated devices out of eligible devices.
8. **PagerDuty recovery could not resolve the incident it opened**, because the dedup keys differed.
9. **The MD5 fingerprint parser** mis-read the "d5" in a "MD5 aa:bb" prefix as hex.

## 2. Fixes (B01–B24)

| ID | Fix |
|---|---|
| B01 | The appliance clock is calibrated from the HTTP `Date` header (`lib/serverClock.ts`), so every relative time in the app uses appliance time. Absolute UTC and local times appear on hover. `/ncm/health` reports NTP sync state. The UI shows NTP-unsynced and browser-skew banners. Future `last_scheduled_at` is treated as a clock anomaly. |
| B02 | Readiness is enforced server-side for single jobs, bulk jobs, `/config-fetch` and the scheduler, returning structured `readiness_blocked` details. The UI shows the blockers with direct fixes. Eligibility now controls coverage only (explicit decision). An inactive worker blocks a backup; an unknown worker state does not. |
| B03 | Bulk settings are partial: only ticked fields change. Collection state and timezone are preserved. A dry-run preview shows before/after per device, and invalid merged states are rejected atomically. Bulk-assign keeps collection state unless it is set explicitly. |
| B04 | Deleting an in-use profile returns 409 with the affected devices. The UI requires choosing a replacement, reassigns in the same transaction, and audits the change. |
| B05 | Runs record job, stage, failure category and retryable flag. Fleet and device job views show the full attempt timeline with recommended actions. |
| B06 | Failures are classified as permanent (trust, authentication, driver, privilege, credentials; no retry) or transient (retried). Missing trust fails before any connection. One open incident per device holds an occurrence count, one alert and one notification. A successful capture closes it, resolves the alert (including legacy per-attempt alerts) and sends one recovery notification. |
| B07 | Bulk timezone choice: keep each device's timezone or override. The per-device next run is computed by the same `ncm_schedule.next_due` the scheduler uses, with DST handled. |
| B08 | Schedule summaries now distinguish every-N-hours, daily and weekly schedules, with next-due and overdue indicators. |
| B09 | Help text is shared: the scheduler checks every minute, and retention text explains protected versions. |
| B10 | `POST /ncm/bulk-jobs` returns a per-device outcome: queued, already active, blocked (with reason), not enrolled or not found. The toast severity matches the outcome. |
| B11 | The bulk bar shows "N selected (M hidden by filters)", with a Review selection dialog. The selection is reconciled against the server and devices that disappear are pruned. |
| B12 | The channel query has loading, error, empty and read-only states. Viewers see destination names from `/ncm-summary`. Unsupported channels are listed by name. |
| B13 | Refresh invalidates all NCCM queries, and every failed section has its own retry. |
| B14 | The profile form shows whether an enable secret is saved and offers an explicit "Clear saved enable secret" (`clear_enable_password`). |
| B15 | The viewer has a real Retry button, and Close clears the selection. |
| B16 | Version history is server-paged with a true total, plus a "Protected recovery points" filter. |
| B17 | Baseline-protected versions show a lock ("Protected by baseline history") instead of Unpin. |
| B18 | `min-w-0` grid children and bounded scroll containers; devices show as cards under 768 px. Verified at 390 × 844 with no page overflow. |
| B19 | Settings are read-only by default with an explicit Edit mode, Save/Cancel, an unsaved-changes badge, and leave/tab/unload guards. Remove has an impact confirmation. Viewers cannot edit. |
| B20 | The server clamps the page, and the UI mirrors it. |
| B21 | The page window has first/last controls and "Page X of Y" with `aria-current`. |
| B22 | The driver catalog exposes capabilities. Unsupported capture types are disabled with a reason, at least one type is required, auto-detect says its capability is unresolved, and fields validate inline. |
| B23 | Cached data is kept after a background error, behind a stale banner with the last-refresh time. Mutating actions are disabled while stale. |
| B24 | Semantic colours (red / amber / green only for fresh-validated / blue / grey) with icon and text. Coverage 0 shows as red. Status bars use tones. Labels are associated in FormField, checkboxes and icon buttons are named, day toggles use `aria-pressed`, and running/startup are always spelled out. |

## 3. Features (F01–F14)

| ID | Status | Delivered |
|---|---|---|
| F01 | **Implemented** | Compliance engine (migrate-131): versioned policies and rules with 5 check types, including section-aware checks for Cisco/Fortinet nesting. Nine built-in starter packs. Evaluation on every validated capture plus daily. Evidence is redacted and line-numbered. Exceptions have owner, reason and expiry (at most 1 year). Daily trend snapshots. UI covers overview, results, policies, rule tester and exceptions. Regular expressions are checked against catastrophic-backtracking shapes. |
| F02 | **Partial** | Aruba AOS-CX, ArubaOS-Switch/ProCurve and Mobility Controller collectors, verified against an SSH simulator. F5 stays **unsupported**: netmiko's TMOS preparation changes persistent CLI preferences, and text is not a UCS archive. The support matrix reports evidence per driver from this appliance's own captures and restore tests. |
| F03 | **Implemented (evidence recording)** | Versions store driver and artifact format; drivers declare scope and omissions. Restore tests (passed/partial/failed, method, target, scope) are recorded, protect the version from pruning, and show as a separate badge. NCCM still never writes to devices. |
| F04 | **Implemented** | Managed SSH trust (migrate-130): probe the presented key without logging in, then require an independently read SHA256/MD5 fingerprint, a verification method and a note. Replacing a changed key needs a reason, and the old key is retired, not deleted. Everything is audited under a new `ncm.trust` permission. Readiness onboarding links straight to each fix. |
| F05 | **Not implemented** | Off-site replication and appliance-loss restore still follow the Phase A runbook (`NCCM-Phase-A-operations.md` §5). This needs a destination/credential design decision. |
| F06 | **Implemented** | Fleet jobs and attempts, queue depth, worker/timer state, due/overdue, delivery history with channel names, and deliberate delivery replay. |
| F07 | **Not implemented (by design)** | No device-write path was added. Restore evidence and diffs support manual recovery plans; governed change execution needs its own approval-workflow design. |
| F08 | **Implemented** | Syslog (`network_events`) and trap (`evaluate-trap` hook, recorded even during maintenance) configuration-change detection. Also `POST /devices/{id}/ncm-change-events` for change-management systems. Backups are debounced (2 min quiet, 15 min cap) with the readiness gate. A user is attributed only when the device message names one. |
| F09 | **Implemented** | UI for comparing configurations across devices, with compatibility warnings and the raw toggle only for `ncm.export`. |
| F10 | **Implemented** | Archive search over latest configurations. It runs on **redacted** text unless the user has `ncm.export`, so it cannot reveal secrets; regex search needs `ncm.manage`. The diff view has unified and side-by-side modes, line numbers, in-diff search, and sensitive/redacted/normalized badges. The config viewer has line numbers and find. |
| F11 | **Implemented** | Baseline list (active and retired) with drift state, retirement with a reason (protection kept), and a searchable, exportable audit workspace. |
| F12 | **Implemented** | "Configuration Protection" report preset: 6 sections in PDF and schedulable (also fixed `rum_usage` never being schedulable), plus CSV export of the device list. |
| F13 | **Partial** | SSH-key profiles (Ed25519/ECDSA/RSA, passphrase, fingerprint), rotation policy with overdue flags, and a usage/impact review before deletion. No vault integration. |
| F14 | **Partial** | Server-side filtering, paging, sorting and facets; a single-device summary endpoint (the device page no longer loads the whole fleet); an `ids` lookup. Per-site concurrency and remote collectors are not done. |

## 4. Verification performed
- 123 NCCM tests: 67 existing (adapted where contracts changed on purpose) and 56 new. They run against a disposable PostgreSQL 16 cluster via `scripts/validate-ncm-integration.py`. The full backend unit suite also passes: 1088 passed.
- Migrations 130/131 apply cleanly on a fresh full schema with `run-migrations.py --include-init`, and `lint-migrations` passes.
- End-to-end on a dev stack (private DB, dev API and worker, SSH simulators for Cisco IOS, a paging FortiGate and Aruba AOS-CX):
  - enroll → blocked by readiness → register trust through the UI → real netmiko capture (running + startup) → compliance results;
  - a wrong-password device gives 1 attempt, 1 incident and 1 alert;
  - a transient FortiGate failure gives 3 attempts → incident → recovery resolves it;
  - a syslog `%SYS-5-CONFIG_I` event gives a debounced `event` job with attribution;
  - bulk preview with mixed timezones;
  - 390 px layout.
- Production build (`vite build`) and `tsc -p tsconfig.app.json` pass.

## 5. Operator actions after deployment
- Run the migrations (130, 131) as usual; they are additive.
- Grant the new permissions `ncm.trust` and `ncm.compliance` to non-admin roles as appropriate. Admin roles get them automatically.
- Register SSH trust for each enrolled device through the new workflow, using console-verified fingerprints.
- Repair NTP on 10.11.40.31 (`NTPSynchronized=no`), and check the reviewer's workstation clock.
- Review the built-in compliance packs before relying on them. They are starter checks, not a certification.
