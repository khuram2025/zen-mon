# ZenPlus Schema Convergence

Date: 2026-08-06 (supersedes the 2026-05-06 migration-runner design)

## The failure this design exists to prevent

An appliance reported version 1.6.0 and looked healthy. Its poller logged
`SNMP cycle complete: 6 ok, 0 errors` and then failed every write with
`Table zenplus.snmp_metrics does not exist`. CPU and memory showed `—` on
`/devices` indefinitely. Ping worked, because `ping_metrics` happened to exist.

Three independent defects combined:

1. **Delivery.** A release shipped only the migrations that release introduced.
   An appliance that skipped a release, or a release whose author forgot
   `--migration`, never received the SQL file at all — and had no way to catch
   up later.
2. **A ledger that was trusted instead of verified.** The ClickHouse sync
   *baselined* a hardcoded list of legacy migrations — recording them as applied
   without running them — whenever the ledger was empty. On an appliance that
   had never received `migrate-004-snmp-clickhouse.sql`, that stamped a lie into
   the ledger, and the table was never created.
3. **A health check that proved nothing.** The updater declared success once
   `/api/v1/system/health` returned 200. That says the API process started. It
   says nothing about whether the database has the tables that process needs —
   so the version marker advanced past a schema that could not support it.

Two appliances then reported the same version while running different code and
different schema, with nothing in the check-in payload able to reveal it.

## The four rules

1. **Every release ships every migration.** Shipping a migration is not running
   one. The runners keep a ledger per engine and apply only what is missing.
2. **Order comes from `scripts/migrations.lock`, not from filenames.** Migration
   numbers are not unique (two 016s, two 030s, two 031s, two 039s, two 043s, and
   one date-stamped file), so a filename sort is not a sequence. The lockfile is
   append-only and its *line order* is release order.
3. **Never trust the ledger alone — probe.** Before recording a migration as
   applied, check that the objects it creates actually exist. Before re-running
   one, check that doing so cannot duplicate data.
4. **The version marker moves last.** Code, schema, and version advance together
   or not at all.

## Components

| Path | Role |
| --- | --- |
| `scripts/migration_order.py` | Ordering + static analysis. Single source of truth, dependency-free, imported by everything below. |
| `scripts/run-migrations.py` | PostgreSQL runner. Ledger: `schema_migrations`. |
| `scripts/ch_migrate.py` | ClickHouse runner. Ledger: `zenplus.schema_migrations`. |
| `scripts/sync-schema.py` | Orchestrator + gate. Runs both, writes `.schema-status.json`, exits non-zero on unresolved drift. |
| `updater/schema_gate.py` | Updater-side wrapper; its verdict decides whether the version marker moves. |
| `updater/clickhouse_sync.py` | Thin adapter kept so an appliance on the *old* updater still heals on the first update that carries this change. |

## How a migration is classified

For each migration not recorded in the ledger, both runners ask the database
what already exists:

| Ledger | Objects present | Action |
| --- | --- | --- |
| recorded | yes | skip |
| not recorded | yes | **baseline** — record without running |
| not recorded | no | **apply**, then record |
| recorded | no | **heal** — the ledger is lying; re-apply (ClickHouse) |

Two cases refuse to guess and are reported instead:

- A migration that is partially present and would duplicate rows on replay.
- A migration with nothing probeable that also inserts rows.

Both surface as `unresolved` and fail the gate, because a wrong guess either
doubles seed data or leaves a silent gap.

### Why "probe" and not "assume"

An appliance installed before tracking existed has a complete schema and an
empty ledger. Without probing, all 60 PostgreSQL migrations would look pending
and be re-run — seven of them insert seed rows. Probing turns an unanswerable
"has this run?" into an answerable "is it here?". On a fully-provisioned
appliance with an empty ledger the result is 32 baselined, 28 applied (all
guarded column/constraint work), 0 unresolved.

## Replay safety

`migration_order.is_replay_safe()` derives the replay decision from the SQL
rather than a hand-maintained list — the hand-maintained list is exactly what
went stale. Guarded idioms it recognises:

- `IF NOT EXISTS` / `IF EXISTS` on DDL
- `DROP CONSTRAINT IF EXISTS x` followed by `ADD CONSTRAINT x`
- `INSERT ... ON CONFLICT` / `INSERT ... WHERE NOT EXISTS`

A bare `INSERT` is never replay-safe. New ClickHouse migrations **must** be
replay-safe; the release builder rejects them otherwise, because healing a false
ledger entry works by re-running the file.

## Commands

```bash
# Is this appliance's schema consistent with its code? (read-only, exit 2 on drift)
sudo /opt/zenplus/scripts/sync-schema.py --check

# Converge it
sudo /opt/zenplus/scripts/sync-schema.py

# One engine only, machine-readable
sudo /opt/zenplus/scripts/sync-schema.py --engine clickhouse --json

# PostgreSQL ledger status on its own
sudo -u postgres python3 /opt/zenplus/scripts/run-migrations.py --status
```

The verdict is written to `/opt/zenplus/.schema-status.json` and reported at
check-in as `schema_status`, alongside `dashboard_build` (the served JS bundle
filename) so version drift is visible fleet-wide rather than only in one
appliance's logs.

## Adding a migration

```bash
# 1. Write scripts/migrate-NNN-description.sql (next free number; -clickhouse suffix for CH)
# 2. Record it
python3 scripts/build-release.py lint-migrations --update-lock
# 3. Commit the migration and scripts/migrations.lock together
```

The linter rejects, at build time:

- an edit to an already-released migration (checksum drift)
- a reused sequence number
- a deleted migration that appliances may not have applied
- a new ClickHouse migration that is not replay-safe

## Release integration

`build-release.py` emits a `run_hook` step for `scripts/sync-schema.py` on
**every** release, not only schema releases. It runs after `apply_code` has
landed the complete migration set and before services start. A non-zero exit
fails the step, which triggers the manifest's rollback.

`--migration FILE` still works but is now emphasis, not delivery: it adds an
explicit `run_migration` step to the manifest. Forgetting it can no longer
strand an appliance.

## Failure modes and what they mean

**`changed migrate-NNN.sql checksum differs from applied record`** — a released
migration was edited. That is a release problem, not a database problem. Add a
new forward-only migration instead. For a single blocked appliance, after a
backup and schema inspection, an operator may update
`schema_migrations.checksum` to match the shipped file if the database already
reflects the intended schema. Never delete ledger rows or re-run old migrations
blindly.

**`unresolved ... cannot verify, inserts rows`** — the runner cannot tell
whether the migration ran and re-running it would duplicate data. Inspect the
table, then either apply it by hand or insert its ledger row.

**`clickhouse: ... partially applied and not replay-safe`** — some of the
migration's objects exist and some do not. Create the missing objects by hand,
then re-run the sync.

**Schema gate fails the update** — the appliance rolls back to the previous code
and stays on the previous version number. An appliance on an older version with
a consistent schema is a working appliance; a half-migrated one is not.

## Test coverage

- `server/tests/test_schema_convergence.py` — ordering, static analysis,
  ClickHouse baseline/heal/apply/unresolved paths, gate verdicts. Includes a
  regression test that reproduces the original field failure: ledger claims
  `migrate-004-snmp-clickhouse.sql` was applied, `snmp_metrics` is missing, and
  the sync heals it.
- `server/tests/test_migration_runner.py` — PostgreSQL discovery order,
  baselining on evidence, checksum drift, unverifiable-insert refusal.
- `server/tests/test_migrations_lint.py` — every build-time guard above.
