# v1.23.10 — required upgrade paths and migration safeguards

Minimum installed version: **1.23.9**. Package scope: appliance-only; existing agent packages are preserved.

Zentryc now resolves required intermediate releases instead of returning no update when the latest release has an unmet minimum version. It honors every bridge's latest rollout, product, architecture and failure limit. Missing or paused prerequisites stop the complete chain. New, manual and automatic rollout promotion verifies prerequisite archive signatures, hashes, signed metadata, default age eligibility and audience coverage.

The appliance checks signed and offered prerequisites at the shared update entry point, runs a signed preflight before stopping services, reloads its newly installed updater between releases, and preserves the installed version marker until schema verification succeeds. Release tooling requires the full locked migration inventory and a database schema gate before service startup. Schema verification fails closed on missing tooling, malformed output or a nonzero exit.

Includes the v1.23.8 HTTP IP selection/connection diagnostics and v1.23.9 device degraded-status explanations. Configured monitoring thresholds and local certificates are preserved.

Required path from v1.23.3: **1.23.7 -> 1.23.8 -> 1.23.9 -> 1.23.10**. Cumulative migrations are evaluated on every hop. Older and unknown-version installations require the appropriate supported source state; do not fabricate version markers.

See [the release runbook](ota-upgrade-path-runbook.md) and scripts/release-policy.json for future releases. Full-rollout promotion follows genuine local v1.23.9 canary verification; remote fleet installation is measured through check-ins and update reports.

## Deployment evidence — 9 September 2026

- Built from GitHub main commit **94cc086**. The signed artifact was not rebuilt after publication.
- Release ID: 97f368bf-6d12-4ad2-abe0-e0d6ed41f71e.
- Package SHA256: a802b228d443526ef756662dc5b052a9044aa55101e6b358404a590230751c19; size 27,996,189 bytes.
- Genuine local canary: **1.23.9 -> 1.23.10**, completed 12:12:22 UTC. All 23 recipe steps passed; PostgreSQL and ClickHouse pending counts were zero; API health returned 200; API, poller, NetFlow collector and nginx remained active.
- Validation: **152 appliance/release regression tests**, migration lint, signed package verification and production dashboard build passed. **29 staged Django tests** passed, including existing ZenShield contracts and signature, tampering, expiry, prerequisite and promotion tests. Chain planning was tested for source versions 1.23.3 through 1.23.10; these selector tests are distinct from the real 1.23.9 canary installation.
- Zentryc selector and new/manual/automatic promotion gates deployed; Django and rollout workers restarted. Existing bridge archives and the complete final chain passed signature, hash, signed-metadata and default age checks.
- Full rollout for v1.23.8: d011049c-d715-4d2a-a61b-089a2904fc3d.
- Full rollout for v1.23.9: 221f7011-7d64-4c1b-bea7-87e64bbdf0e4.
- Full rollout for v1.23.10: b253aff6-05e4-402d-b6a3-e9a0bf84d360, activated 12:13:58 UTC. All three target 100% with no group restriction; v1.23.7 already had full rollout. Automatic promotion was not enabled for these releases.
- Live fleet selection verified: older known-version appliances receive v1.23.7 first, and v1.23.7 receives v1.23.8. The local v1.23.10 appliance's real authenticated check-in and explicit update check both returned no further update. One registered appliance has an unknown version and is intentionally blocked pending diagnosis. Full rollout is availability, not evidence that every appliance has installed the release.

Public release: https://zentryc.com/ota/releases/97f368bf-6d12-4ad2-abe0-e0d6ed41f71e/
