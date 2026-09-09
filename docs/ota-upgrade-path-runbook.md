# ZenPlus required upgrade paths and release checklist

Maintained with the release source. Introduced in v1.23.10 on 9 September 2026.

## Contract

A release's signed min_version names its minimum installed prerequisite. If an appliance is below it, Zentryc resolves that exact published bridge and recursively resolves its prerequisites. It offers only the first required step. After a successful update the appliance rechecks, then starts the newly installed updater in a fresh process before the next hop. A failed download, prerequisite, migration, schema check or health check stops progression.

Cumulative releases ship the full migration set. Installing every historic release is unnecessary: migration runners use scripts/migrations.lock in original release order and the PostgreSQL/ClickHouse ledgers to apply pending work. Required one-time OS, updater, data-transform or trust-key transitions must be represented by min_version and retained as a published, accessible bridge. Severity alone does not make a release a bridge. Future cumulative releases include earlier security fixes; never omit a required one-time operation without retaining its prerequisite.

Current tested chain: **1.23.3 -> 1.23.7 -> 1.23.8 -> 1.23.9 -> 1.23.10**. An appliance already at a later point begins after that point. v1.23.7 is the cumulative entry for this chain; v1.23.8 and v1.23.9 keep their immutable signed minimums. Unknown or corrupt version markers require operator repair, not a guessed upgrade or downgrade.

## Required safeguards

- Server selects within the appliance's product and architecture. Every bridge must be published and eligible under its latest rollout. Paused, aborted, completed, wrong-group, zero-percent and missing prerequisites block the entire path. Three failed attempts block further automatic attempts; investigate instead of routing around the bridge.
- The common appliance run_update function checks forward direction and minimum source version before download. After signature verification it compares the offered and signed version/minimum with code/.version before execution.
- The signed first-step ota-preflight.py checks prerequisites before stopping services, including when an older updater executes the new recipe. v1.23.10 updater leaves the installed version marker unchanged during code replacement and commits it only after verification. On older updaters, existing rollback behavior still applies; do not bypass the central offer or minimum-version check for manual installations.
- Every build must ship the complete append-only migration ledger and tools. The package verifier checks every locked migration's hash, rejects extra/unlocked SQL, and requires schema convergence before restarting services.
- Both database verifiers must return an explicit successful verdict. Missing tooling, unavailable databases, invalid output, nonzero exits or remaining drift fail the update. A successful HTTP response alone does not establish migration success.
- Keep database/code backups and tested restore procedures. ClickHouse nontransactional changes and external OS effects are not universally reversible; new destructive migrations need an explicit recovery plan and prior-version data testing.

## Future release procedure

1. Use a clean, synchronized main checkout. Preserve unrelated development work outside the release checkout.
2. Review scripts/release-policy.json. Its minimum_supported_upgrade_base is the default and enforced floor. Raise it when introducing a new required bridge. Lowering it requires reviewed evidence that a cumulative replacement preserves all required transitions.
3. Add new migration files; never edit, remove or reorder a shipped lock entry. Update migrations.lock deliberately and run migration lint and migration-runner tests.
4. Document the minimum version, bridge rationale, source commit, schema changes, recovery plan and test evidence in docs/release-VERSION.md. Keep packages immutable; rebuilds require a new version.
5. Build and publish initially without rollout: bash scripts/release.sh VERSION "Release notes" normal none MIN_VERSION appliance-only. Appliance-only preserves installed agent packages; choose bundled only when agent artifacts were intentionally rebuilt and tested.
6. Verify the catalog's exact immutable ID, version, signed minimum, SHA256, signature and published state. Check the complete prerequisite path, including package downloads, dates, architecture and rollout coverage. The default appliance manifest age window is 30 days: an expired bridge must be replaced through a reviewed, freshly signed supported path; never disable verification or silently skip it.
7. Upgrade a genuine prior-version canary through the normal updater. Verify backup, all migration steps, schema verdict, API/poller/collector health, version and history. Exercise chain selection and failure-stop tests. Do not call a mocked version marker a real migration test.
8. Promote required bridges first, then the target. Each prerequisite must cover the target audience at 100% before a dependent rollout. Use immutable release IDs and product=ota. New, manual and automatic rollout promotion is guarded on Zentryc by signed artifact and prerequisite checks. Do not enable automatic promotion for a new path without independent canary evidence.
9. Verify real check-in and GET update-check responses at each starting version, plus current-version no-offer behavior. Full rollout means eligible appliances can receive the update; it does not prove every offline or blocked appliance has installed it.
10. Record release/rollout IDs, source commit, package hash, canary from/to versions, test totals and any blocked fleet cohorts in the release evidence. Check failures before expanding or retrying. Pause the latest rollout to stop offers; never delete a required bridge still used by supported versions.

## Zentryc integration source

integrations/zentryc_ota contains the deployed ZenPlus-only selector, artifact promotion guard, regression tests and hash-guarded installer. The website runs a separate Django deployment; these modules are versioned here for reproducibility and must also be deployed there. The installer preserves prior files and refuses changed baselines. ZenAI/ZenShield offer selection stays independent. Run staged Django tests against a disposable database, including the existing ZenShield contract tests, before deployment. Restart/reload the actual Django workers after installing code; a file copy alone is not deployment.

If a bridge is paused, unpublished, missing, expired or repeatedly failing, leave the chain blocked. Repair or publish a reviewed replacement and restore its rollout before offering dependents. Never fake migration-ledger entries or a successful installed-version marker to force advancement.
