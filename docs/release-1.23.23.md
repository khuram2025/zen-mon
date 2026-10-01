# ZenPlus 1.23.23

Consolidation release. It carries the NCCM development review (B01–B24 and new
NCCM features, migrations 130/131) and the Alert Center Notify-column fix on top
of 1.23.22, and it is built so any appliance that skipped 1.23.19 – 1.23.22
converges fully in a single update.

## What is new

- **NCCM**: readiness enforced on every backup path; partial, previewed bulk
  settings; failures classified permanent/transient and correlated into one
  incident per device; managed SSH trust; compliance engine (starter packs,
  exceptions, trends); Aruba AOS-CX / AOS-Switch / Mobility Controller
  collectors; SSH-key profiles with rotation; restore-test evidence; event-
  triggered backups; fleet jobs, audit, archive search and cross-device compare;
  Configuration Protection report preset. New permissions `ncm.trust` and
  `ncm.compliance` (granted through `system.admin`).
- **Alert Center**: Notify and action controls stay inside the table.
- Dependency: axios 1.20.0 (clean install; the release gate's `npm audit` is
  clean).

## Upgrade coverage (1.23.18 → 1.23.21 and every other 1.23.x base)

- Minimum supported installed version stays **1.23.9**.
- The package is cumulative: it replaces the code tree, ships every migration
  (142+) and runs `sync-schema.py`, which applies whatever the appliance ledger
  lacks, in `migrations.lock` order. Dashboard build, Go poller / netflow /
  sensor binaries, systemd units and the nginx ingest hook are reinstalled by
  the same package.
- Migrations an appliance may be missing, depending on where it starts:
  `128` (1.23.19), `129` (1.23.20), `135` (Alert Center), `136` (Services),
  `130`/`131` (NCCM). 130/131 are appended to the lock **after** 135/136
  because the lock is append-only; appliances already on 1.23.22 therefore
  receive them as a normal catch-up.
- Rehearsed: a scratch PostgreSQL built from the 1.23.18 migration set, then the
  1.23.23 set applied — exactly 128, 129, 135, 136, 130, 131 were pending, all
  applied, and a second run was a no-op.
- The alert-engine endpoints are restricted to loopback in the API itself, so
  appliances whose nginx config predates the `install.sh` `/api/v1/alert-engine/`
  block are still protected.

## Behaviour changes carried from 1.23.22

Snoozed alerts stay open under a Suppressed tab; alert triage needs
`alerts.acknowledge` and rule edits need `alerts.manage`.
