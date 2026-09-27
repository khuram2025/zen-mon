# ZenPlus Agent 1.13.0

Windows x64, built 2026-09-27. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Packaging release: the agent now ships in two flavours so servers that only
need infrastructure monitoring no longer carry the APM toolkit. Includes every
1.12.10 security fix. Not yet published to the stable channel or to any
appliance.

## Packages

| Package | 1.12.10 | 1.13.0 | Installed size |
| --- | --- | --- | --- |
| Server monitoring (`zenplus-agent-core-1.13.0`) | n/a | 26 MB | about 44 MB, 5 files |
| Server monitoring + APM (`zenplus-agent-1.13.0`) | 205 MB | 93 MB | about 210 MB |

An existing 1.12.x installation occupies about 430 MB. About 216 MB of that is
a second copy of the setup program kept as the uninstaller, which is gone in
1.13.0.

## Changes

- **Two packages.** The core package installs server monitoring only. The full
  package adds the APM gateway and .NET, Java, Node.js and Python
  instrumentation. It keeps the historical `zenplus-agent-<version>` names, so
  1.12.x agents (which all carry APM) upgrade to it and keep APM.
- **Compressed installers.** The setup program embeds one compressed archive
  instead of raw files, and still verifies every file before installing.
- **Small uninstaller.** A payload-free build of the setup program is
  installed as `ZenPlusAgentSetup.exe` instead of a copy of the whole package.
- **Safe switching.** Install the full package over core at any time to add
  APM. The core package refuses to replace an installation that still uses APM
  (an APM profile or instrumented applications), rather than breaking those
  applications on their next restart. Both MSIs share one UpgradeCode and allow
  same-version upgrades, so switching flavour never leaves two products
  installed.
- **Flavour-aware self-update.** Agents update within their installed flavour.
  They refuse a manifest that cannot name that flavour, and a core agent falls
  back to the public channel if its controller only publishes full packages.
- **APM on server-only installs.** An APM profile on a core installation
  reports "not installed" instead of repeatedly requesting an ingest
  credential. Settings refuses APM profiles there with an explanation.

## Controller and dashboard (same release branch)

- Migration `migrate-122-agent-package-flavors.sql` adds `agent_packages.flavor`
  (existing rows become `full`) and makes uniqueness and "latest" per flavour.
- The package store recognises `zenplus-agent-core-*` files. It keeps one
  package per version and flavour (preferring the `.exe`) instead of
  quarantining a version whose `.exe` and `.msi` are both present.
- The manifest and download endpoints accept `flavor` (default `full`, so
  older agents are unaffected). `install.ps1` installs the core package for the
  `infrastructure` profile, which is now its default.
- The Download Agent dialog offers both packages. The public site needs the
  new aliases `ZenPlusAgentCoreSetup-latest.exe` and
  `ZenPlusAgentCore-latest.msi` (see `docs/deployment.md`).

## Validation

- All Go package tests and `go vet ./...` passed. New tests cover payload
  validation per flavour, the core-over-APM refusal, uninstaller staging,
  flavour selection (including a legacy controller that only answers with the
  full package), the not-installed APM state and the Settings refusal.
- Server: 71 tests pass, including the new flavour sync test. The migration
  was applied twice to a throwaway PostgreSQL 16 database with a pre-flavour
  table. Existing rows became `full`, core and full rows of one version
  coexisted, the new upsert key worked, and invalid flavours were rejected.
- The dashboard type-checks with `tsc`.
- Both setups and MSIs, the gateway, the uninstaller and all agent binaries
  have valid, timestamped Zentryc LLC signatures. `zenplus-agentctl version`
  reports 1.13.0. Both setups pass `/verify-payload`. The pinned self-update
  verifier accepts all four packages and rejects a tampered copy. Microsoft
  Defender found no threats.

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `zenplus-agent-core-1.13.0.exe` | 27524528 | `62e5f39a0f1e6a506135da15e2fd90bc0f7b795de9e78c58d04c841761fa4c71` |
| `zenplus-agent-core-1.13.0.msi` | 28753920 | `93b80c894e49d655d1f719d1b67afddf05e45f00bf8ff55e5024028e213f7d9b` |
| `zenplus-agent-1.13.0.exe` | 97050544 | `cb8093a81ff13967fb23686ee474137a0713135553e0d441a3d32e378de92104` |
| `zenplus-agent-1.13.0.msi` | 98349056 | `ed25c2eee3ee9bf09f250d6fa226adc64fa6b62cf3321f0a590d2e64f885ec25` |

Limits: no live install, upgrade or flavour switch has been run on a machine
yet, and the controller changes are not deployed. Run the upgrade matrix in
`docs/production-release-checklist.md` on a test VM before publishing.
