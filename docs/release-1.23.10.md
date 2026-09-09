# v1.23.10 — required upgrade paths and migration safeguards

Minimum installed version: **1.23.9**. Package scope: appliance-only; existing agent packages are preserved.

Zentryc now resolves required intermediate releases instead of returning no update when the latest release has an unmet minimum version. It honors every bridge's latest rollout, product, architecture and failure limit. Missing or paused prerequisites stop the complete chain. New, manual and automatic rollout promotion verifies prerequisite archive signatures, hashes, signed metadata, default age eligibility and audience coverage.

The appliance checks signed and offered prerequisites at the shared update entry point, runs a signed preflight before stopping services, reloads its newly installed updater between releases, and preserves the installed version marker until schema verification succeeds. Release tooling requires the full locked migration inventory and a database schema gate before service startup. Schema verification fails closed on missing tooling, malformed output or a nonzero exit.

Includes the v1.23.8 HTTP IP selection/connection diagnostics and v1.23.9 device degraded-status explanations. Configured monitoring thresholds and local certificates are preserved.

Required path from v1.23.3: **1.23.7 -> 1.23.8 -> 1.23.9 -> 1.23.10**. Cumulative migrations are evaluated on every hop. Older and unknown-version installations require the appropriate supported source state; do not fabricate version markers.

See [the release runbook](ota-upgrade-path-runbook.md) and scripts/release-policy.json for future releases. Full-rollout promotion follows genuine local v1.23.9 canary verification; remote fleet installation is measured through check-ins and update reports.
