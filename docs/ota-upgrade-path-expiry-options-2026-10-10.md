# OTA upgrade-path expiry: why no rollout can be created, and the options

Written 2026-10-10 while releasing 1.24.0. This is a decision document. It changes nothing on the Zentryc portal, in the release builder or in the release policy.

## What happened

- ZenPlus 1.24.0 was built from the tag `v1.24.0`, verified offline (`signature: valid`, 1,565 signed files, source commit `8ec7fe7`, appliance-only) and published to the Zentryc catalog (release id `4f4c691c-34ca-4693-a122-e040d1c7de8c`) with `--min-version 1.23.9`.
- Creating the canary rollout then failed on the portal:

  `Upgrade path cannot be promoted: Prerequisite 1.23.7 is outside the default manifest age window; publish a supported replacement path`

- A published release with no rollout policy is not offered to any appliance (`rollout_allows` in `integrations/zentryc_ota/upgrade_path.py`), and the runbook's own first step is "build and publish initially without rollout" (`docs/ota-upgrade-path-runbook.md`, step 5). So 1.24.0 is safely dormant. Nothing was retried and nothing was changed on the portal.

## Why it failed (verified in the source)

`integrations/zentryc_ota/rollout_guard.py` (`validate_rollout_path`) runs on every new, manual and automatic rollout promotion. It plans the upgrade path from version `0.0.0` to the release being promoted, so it includes every bridge named by the signed `min_version` links, and for every step it requires:

1. the package hash and the signature of `manifest.json` to verify;
2. the signed `version`, `min_version` and `arch` to equal the catalog;
3. the manifest `release_date` to lie within the last **30 days** (hard-coded);
4. for bridges: an active rollout at 100% that covers the target group.

The chain behind every current release is `1.24.0` (min 1.23.9) -> `1.23.9` (min 1.23.8) -> `1.23.8` (min 1.23.7) -> `1.23.7` (no minimum). The portal lists these three bridges as created on **2026-09-09** (09:40, 11:09 and 11:50 UTC); the signed manifest dates were not read, but they are the same day. Their 30 days ended on **2026-10-09**. Release 1.23.35 was published on 2026-10-08, inside the window; 1.24.0 is the first promotion attempted after it closed.

The builder cannot avoid the chain: `scripts/release_policy.py` forces every release's `min_version` to be at least `minimum_supported_upgrade_base` in `scripts/release-policy.json` (now `1.23.9`) and below the release, so every release inherits the 1.23.9 -> 1.23.8 -> 1.23.7 chain. Signed minimums are immutable, so older releases cannot be re-pointed either.

## What it blocks

- **Every new rollout of every release** (canary, percentage or full), including an urgent security fix, until the chain is repaired.
- **Promotion of rollouts already in flight** (manual and the automatic canary-to-full step, which logs `upgrade_path_promotion_blocked`).
- It does **not** change what appliances already offered a rolled-out release receive, and it does not affect appliances that are already on 1.23.9 or newer for releases that are already at 100%.

This will recur: with a static chain, whichever release sits at its base ages out 30 days after it was signed. Any fix must also say how the chain base is kept fresh, or the guard has to stop measuring the base.

## What must not be done

- Do not disable the check, widen the 30-day constant, or skip steps to push 1.24.0 through. The runbook says "never disable verification or silently skip" and "Never delete a required bridge still used by supported versions". An appliance that needs an expired bridge would also fail locally, because the updater itself refuses manifests older than its `max_manifest_age_days`.
- Do not unpublish the old bridges: `plan_upgrade` then reports "Required bridge ... is missing or unpublished" for every release whose signed minimum names them.
- Do not rebuild 1.24.0 under the same number. Packages are immutable; a rebuild is a new version.

## Options

### A. Judge the path from the supported floor, not from 0.0.0 (smallest change)

Change `validate_rollout_path` to plan from the release's own signed `min_version` (equivalently the policy floor) instead of `0.0.0`. For 1.24.0 the path becomes just `[1.24.0]` and the hash, signature, field-match and age checks all still run on it. Appliances below the floor are then explicitly unsupported for direct rollout (they must be taken to 1.23.9 first, which the existing bridges can still do only while the updater accepts their manifests, i.e. not any more).

- Keeps verification for everything a supported appliance installs. It removes the requirement that an unsupported population (older than the floor) stays reachable.
- Needs: the owner declaring the floor is the oldest supported version; evidence of the fleet's versions (see below); a reviewed change to `rollout_guard.py`, its tests (`test_upgrade_path.py` currently asserts that an expired bridge is rejected; that test would be re-written for the new rule), and a hash-guarded deployment to the Django site with `install.py`.
- 1.24.0 as built and published could then be promoted without any rebuild.
- It permanently ends the recurring expiry, because old bridges leave the measured path.

### B. Replace the chain base with a fresh cumulative entry (what the error message suggests)

Publish a freshly signed cumulative release that becomes the new chain entry (`min_version` empty), declare it the floor, and give later releases `min_version` equal to it.

- Needs a builder and policy change: `validate_policy` currently never allows an empty `min_version` and never allows a floor below the recorded one, so a "chain entry" concept (and its audit) has to be added. It needs reviewed evidence that the one-time transitions carried by 1.23.7 to 1.23.9 are either complete on every supported appliance or included in the entry.
- 1.24.0 as built cannot use it (its signed minimum is 1.23.9). It would be re-issued as 1.24.1 with the new entry as minimum.
- The entry expires 30 days after it is signed, so this option needs a standing rule to re-base before every expiry (a release cadence under 30 days or a scheduled re-issue). Without that it recurs.

### C. Hold

Leave 1.24.0 published and dormant; ship nothing else until A or B is decided. This is safe for 1.24.0 but blocks all releases, including fixes.

### Not recommended: make the window configurable or longer

It weakens the control the runbook says never to weaken, and the updater's own manifest-age rule on the appliance would still refuse the stale bridges.

## Evidence the owner needs to decide

1. The distribution of `current_version` over active appliances on the portal. Is anything below 1.23.9? Below 1.23.35?
2. What the one-time transitions in 1.23.7 to 1.23.9 were (updater, trust key, data transform) and whether any appliance still needs them.
3. Whether releases are expected at least monthly (then B is sustainable) or not (then A).
4. Who deploys the portal change: `integrations/zentryc_ota/install.py` is hash-guarded and the site is a separate Django deployment.

## Where 1.24.0 can go after the decision

- After A: create the canary rollout for the published 1.24.0 (`zentryc-release ... rollout --version 1.24.0 --stage canary --group canary`), test on the reference appliance, then promote.
- After B: build and publish a new version with the new minimum; 1.24.0 stays published and dormant (it cannot be deleted from the catalog by the tools in use).
- In both cases full rollout waits for acceptance of the exact signed package on a separate reference appliance (`/home/zen/releases/README.md`).
