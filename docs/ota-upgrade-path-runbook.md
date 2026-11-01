# ZenPlus required upgrade paths and release checklist

Maintained with the release source. Introduced in v1.23.10 on 9 September 2026.

## Contract

A release's signed min_version names its minimum installed prerequisite. If an appliance is below it, Zentryc resolves that exact published bridge and recursively resolves its prerequisites. It offers only the first required step. After a successful update the appliance rechecks, then starts the newly installed updater in a fresh process before the next hop. A failed download, prerequisite, migration, schema check or health check stops progression.

Cumulative releases ship the full migration set. Installing every historic release is unnecessary: migration runners use scripts/migrations.lock in original release order and the PostgreSQL/ClickHouse ledgers to apply pending work. Required one-time OS, updater, data-transform or trust-key transitions must be represented by min_version and retained as a published, accessible bridge. Severity alone does not make a release a bridge. Future cumulative releases include earlier security fixes; never omit a required one-time operation without retaining its prerequisite.

For releases from 1.24.38, the required Windows installer catalog bridge is
**1.24.36**. The build policy defaults to this minimum and rejects lower values.
A 1.24.29 appliance follows **1.24.29 -> 1.24.36 -> 1.24.38**. The bridge
contains the signed Windows agent 1.14.4 core/full EXE and MSI installers;
subsequent appliance-only packages preserve that catalog. Do not promote
1.24.37 beyond its existing canary: its immutable minimum predates this bridge.
Raising the minimum in the portal alone is invalid because it must match the
signed manifest. Existing 1.24.37 canaries already provisioned with the catalog
can proceed to 1.24.38. Keep the bridge available and validate its eligibility,
signature, hash and age for supported older cohorts before promotion.

Historical chain: **1.23.3 -> 1.23.7 -> 1.23.8 -> 1.23.9 -> 1.23.10**.
An appliance already at a later point begins after that point. v1.23.7 is the
cumulative entry for this chain; v1.23.8 and v1.23.9 keep their immutable signed
minimums. Unknown or corrupt version markers require operator repair, not a
guessed upgrade or downgrade.

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
6. Verify the catalog's exact immutable ID, version, signed minimum, SHA256, signature and published state. Check the complete prerequisite path, including package downloads, dates, architecture and rollout coverage. The default appliance manifest age window is 30 days: an expired bridge must be replaced through a reviewed, freshly signed supported path; never disable verification or silently skip (see Promotion guard scope below: the guard no longer measures bridges older than the signed minimum) it.
7. Upgrade a genuine prior-version canary through the normal updater. Verify backup, all migration steps, schema verdict, API/poller/collector health, version and history. Exercise chain selection and failure-stop tests. Do not call a mocked version marker a real migration test.
8. Promote required bridges first, then the target. Each prerequisite must cover the target audience at 100% before a dependent rollout. Use immutable release IDs and product=ota. New, manual and automatic rollout promotion is guarded on Zentryc by signed artifact and prerequisite checks. Do not enable automatic promotion for a new path without independent canary evidence.
9. Verify real check-in and GET update-check responses at each starting version, plus current-version no-offer behavior. Full rollout means eligible appliances can receive the update; it does not prove every offline or blocked appliance has installed it.
10. Record release/rollout IDs, source commit, package hash, canary from/to versions, test totals and any blocked fleet cohorts in the release evidence. Check failures before expanding or retrying. Pause the latest rollout to stop offers; never delete a required bridge still used by supported versions.

## Promotion guard scope (changed 2026-10-10)

The promotion guard (`integrations/zentryc_ota/rollout_guard.py`) verifies the hash, the signature, the signed version, minimum and architecture, and the 30-day manifest age of **the path that the promoted release's supported appliances take**. By default that path starts at the release's own signed `min_version` (the builder forces it to be at least `minimum_supported_upgrade_base`), so for a release whose minimum is the floor the path is the release alone. Bridge releases older than the floor are not part of it: before this change the guard planned from `0.0.0`, which made the oldest bridge's signing date the limit for every rollout, and from 2026-10-09 (30 days after 1.23.7 to 1.23.9 were signed) no rollout of any release could be created.

From 1.24.38, promotion requires a minimum of 1.24.36 or higher and the guard
plans from 1.24.35 so it verifies the 1.24.36 catalog bridge itself, including
publication and full audience coverage. The older 1.23.9 prerequisite remains
outside this scope. A missing, expired, modified or ineligible catalog bridge
blocks promotion as well as appliance selection. Keep the deployed portal guard
synchronized with this source; increasing a future bridge floor requires review
of both the builder policy and portal guard.

- Appliances below the floor are unsupported for direct updates. Take them to the floor first. The owner confirmed on 2026-10-10 that no appliance is below 1.23.9.
- Every step the guard does plan is still fully verified, including the bridge checks (active rollout, 100%, group coverage) if a release ever names a minimum above the planning start.
- Setting `ZENPLUS_OTA_VALIDATE_FULL_CHAIN = True` in the Django settings restores the previous behaviour (whole chain from `0.0.0`).
- Deploying a changed guard: copy `rollout_guard.py` (and `test_upgrade_path.py`) over `ota/` on the site (`install.py` installs the hooks once and refuses to run again when its backups exist), run the staged Django tests against a disposable database, then restart or reload the Django workers.

## Zentryc integration source

integrations/zentryc_ota contains the deployed ZenPlus-only selector, artifact promotion guard, regression tests and hash-guarded installer. The website runs a separate Django deployment; these modules are versioned here for reproducibility and must also be deployed there. The installer preserves prior files and refuses changed baselines. ZenAI/ZenShield offer selection stays independent. Run staged Django tests against a disposable database, including the existing ZenShield contract tests, before deployment. Restart/reload the actual Django workers after installing code; a file copy alone is not deployment.

If a bridge is paused, unpublished, missing, expired or repeatedly failing, leave the chain blocked. Repair or publish a reviewed replacement and restore its rollout before offering dependents. Never fake migration-ledger entries or a successful installed-version marker to force advancement.
