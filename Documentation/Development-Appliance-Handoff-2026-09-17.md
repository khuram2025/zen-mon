# Development appliance handoff — 17 September 2026

Continuation results and outstanding release gates are recorded in
[Development validation](Development-Validation-2026-09-17.md). The strict
TypeScript build was subsequently repaired; the original baseline observations
below remain historical evidence rather than the latest validation verdict.

The destination appliance is at another location and has not been accessed or
verified in this handoff. This document identifies the source to retrieve,
requirements to provision locally, and the checks needed before continuing work.

## Source baseline

- Repository: https://github.com/khuram2025/zen-mon
- Development branch: `codex/rum-production`
- Application baseline: `865cde7fc7d2cfdc58eab10437d693e1f4f9cbae`
- Scope: all pending source, tests, migrations, documentation and validation
  reports from the originating workspace, including agent uploader resilience,
  network events, SNMP/discovery, NCM assurance/jobs, and dashboard changes.

Use this branch to continue this work. The push did not merge it into `main`,
publish a new OTA release, or install it on another appliance. A default clone
of `main` or the README's installer URL does not establish this baseline.

On the destination, create a separate development checkout rather than switching
the files used by a running installation. These commands assume the target
directory and branch do not already exist; if they do, inspect and preserve
their local work first.

```bash
mkdir -p "$HOME/src"
git clone --branch codex/rum-production --single-branch \
  https://github.com/khuram2025/zen-mon.git "$HOME/src/zenplus-dev"
cd "$HOME/src/zenplus-dev"
git merge-base --is-ancestor 865cde7fc7d2cfdc58eab10437d693e1f4f9cbae HEAD
git status --short --branch
git switch -c codex/remote-appliance-development
```

The ancestry check must exit zero. Use the destination's own GitHub identity;
do not embed credentials in the remote URL. Record `git rev-parse HEAD` with
subsequent test results. Push the new branch with `git push -u origin HEAD`.

## What Git supplies and what the destination must supply

| Item | Required action |
| --- | --- |
| API, dashboard, poller, sensor and Windows agent source | Retrieve the branch above, including tests and build scripts. |
| Python dependencies | Create a local virtual environment and install `server/requirements.txt`; run `pip check`. Retain the bcrypt compatibility pin. |
| Dashboard dependencies | Use Node/npm compatible with the lockfile and `npm ci` in `dashboard`; rebuild on the destination. |
| Go toolchain | Honor both `go.mod` files: poller declares Go 1.24.0 and the agent declares Go 1.26. Use a toolchain that supports the module being built. |
| Database/cache services | Provision PostgreSQL, ClickHouse and Redis for the selected development environment. Integration runners have additional fixture requirements below. |
| Runtime configuration | Provision private environment files, database credentials, service identity/permissions, TLS configuration and appliance registration on the destination. These are not supplied by Git. |
| Existing encrypted data | If authorized data is migrated, transfer the corresponding encryption key securely with a protected recovery copy. A new key cannot decrypt existing NCM/SNMP secrets. |
| NCM device access | Provision authorized device profiles and verified SSH host keys in `/etc/zenplus/known_hosts`; ensure the `zenplus` account can read the required files. |
| Generated packages/binaries | Rebuild or obtain a separately verified signed release. `.zup`, Git bundles, staging archives, compiled validation binaries and dashboard snapshots were excluded from this push. |
| Signing/OTA credentials | Needed only on an authorized release/signing host. They are not a prerequisite for editing source or running isolated tests. |
| Database contents, backups and runtime state | Not transported by the Git push. Choose fresh development fixtures or a separately authorized data migration. |

Do not copy the originating workstation's private credential file into this
repository. Provision access privately on the destination. Preserve the target's
existing appliance identity; copying another appliance's updater registration
is not part of source synchronization.

## Local build and contract checks

From the separate checkout, with Python, Node/npm and Go installed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python -m pip check
(cd dashboard && npm ci)
.venv/bin/python scripts/validate-network-development.py
ZENPLUS_DIR="$PWD" ZENPLUS_API=http://127.0.0.1:1 \
  .venv/bin/python -m pytest server/tests/test_ncm_assurance.py \
  server/tests/test_password_hash_compatibility.py -q
```

The network runner executes synthetic Python contracts, selected Go packages,
dashboard editor contracts and a Vite bundle. Review every result in
`output/network-development-validation/results.json`; a skipped or unavailable
runtime does not count as passing. This does not install application services.

Full TypeScript checking has known outstanding diagnostics documented in the
NCM progress report; a successful Vite build is not a clean TypeScript check.
Windows agent packaging and signing require the Windows build/signing workflow
in [the agent release notes](../ZenPlus_Agent/docs/release-1.12.5.md). The 1.12.5
validation installers were unsigned and must not be treated as a stable release.

## Database and service readiness

For an installed development runtime, use the existing controlled deployment
and schema-convergence workflow. First preserve the destination's local changes,
configuration and recoverable database state. See
[schema convergence](18-MIGRATION-RUNNER.md) and the
[release runbook](15-RELEASE-RUNBOOK.md).

- Ship the complete `scripts/migrations.lock` and migration set. Resolve all
  pending/drifted migrations in lockfile order, including PostgreSQL migrations
  115, 117 and 118, and ClickHouse migration 116. Do not apply only those four to
  an older installation or rewrite historical checksums.
- Verify the installed API source, built dashboard, poller and NetFlow binaries,
  and sensor artifact against the selected build. A version label or Git HEAD
  alone does not establish what running services have loaded.
- Install the NCM backup service/timer, worker service, and delivery service/timer
  from `scripts/systemd`. Configure authorized development device targets and
  notification sinks before enabling scheduled captures or delivery.
- Install the optional syslog receiver unit. Keep its default loopback scope
  until the destination's intended listening interface and source allowlist are
  configured. Installation alone need not enable the receiver.
- Check dependency imports and configuration access under the actual `zenplus`
  service account, not just root. Previous deployments exposed unreadable Python
  package files despite successful root-level checks.
- Verify API/login, database convergence, running services, NCM job processing,
  dashboard routes and intended receiver behavior. Use isolated notification
  sinks during acceptance.

## Integration fixtures and remaining acceptance

`scripts/validate-network-integration.py` requires a separately provisioned
`/tmp/zenplus-network-integration-*` source tree, PostgreSQL 16 on its private
socket/port 15432, isolated ClickHouse on port 18123, and a private fixture
configuration. It does not provision those database servers itself.

`scripts/validate-ncm-integration.py` requires a separately provisioned
`/tmp/zenplus-ncm-integration-*` source tree and PostgreSQL 16 on its private
socket/port 15433 with the `ncm_fixture` role/database. Both runners must use
disposable fixtures, not the installed application's database.

The originating Windows workspace checks on 17 September passed 132 targeted
Python tests with 23 skips, the dashboard editor contracts and the Vite bundle.
Go was unavailable for that final push check. Earlier Linux/agent evidence is
recorded in the linked reports; none certifies this unseen destination.

NCM operational acceptance remains incomplete: the new NCM work was implemented
and fixture-tested, but the progress report does not claim it was deployed to
the production NCM service. Real device captures, model/OS coverage and recovery
acceptance still require the selected lab and authorized device access.

Continue with these records:

- [NCM implementation and remaining work](NCM-Implementation-Progress.md)
- [Network integration fixture evidence](Network-ABC-Appliance-Integration-2026-09-10.md)
- [Receiver validation and runtime requirements](Network-Receiver-Validation-2026-09-15.md)
- [Publication status of the earlier 1.23.12 package](Network-Publication-2026-09-16.md)
- [Multi-machine development workflow](26-GITHUB-MULTI-MACHINE-AND-PUBLISHING-RUNBOOK.md)

Mark the destination ready only after recording its exact source commit,
dependency/build results, fixture results, configuration readiness, schema
verdict and runtime checks. Source availability is confirmed; destination
readiness is pending execution there.
