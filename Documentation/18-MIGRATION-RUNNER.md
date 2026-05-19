# ZenPlus Migration Runner

Date: 2026-05-06

## Purpose

ZenPlus now has a PostgreSQL migration runner that tracks applied migrations in a `schema_migrations` table. This replaces the unsafe pattern of blindly running every `migrate-*.sql` file during every install or update.

The runner applies PostgreSQL migrations in sorted filename order, skips already-applied files, detects checksum drift, and records duration.

## Command

```bash
python3 /opt/zenplus/scripts/run-migrations.py --database zenplus
```

Useful options:

```bash
python3 scripts/run-migrations.py --status
python3 scripts/run-migrations.py --dry-run
python3 scripts/run-migrations.py --include-init
python3 scripts/run-migrations.py --database zenplus --user zenplus --host localhost
```

## Tracking Table

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER NOT NULL
);
```

## Rules

- PostgreSQL migrations are files matching `scripts/migrate-*.sql`.
- ClickHouse migrations are excluded by the runner for now and remain on the existing ClickHouse path.
- Already-applied migrations with the same checksum are skipped.
- Already-applied migrations with a different checksum are reported as checksum drift and are not re-applied.
- New migrations should be idempotent where possible.
- Released migration files are immutable. Do not edit a migration after it has shipped; add a new numbered migration that moves the schema forward.
- Release packages must include only the migration files introduced by that release. Do not bundle the historical migration set in an OTA package.

## Updater Integration

The OTA updater `run_migration` step now prefers the tracked migration runner for PostgreSQL migrations when `/opt/zenplus/scripts/run-migrations.py` is available. It falls back to the legacy direct `psql -f` path only if the runner is unavailable.

## Release Workflow

Before building or publishing a release, the release builder checks
`scripts/migrations.lock`. The lockfile records the SHA-256 digest of every
released `migrate-*.sql` file and makes migration edits fail before a package is
published.

When adding a new migration:

```bash
python3 scripts/build-release.py lint-migrations --update-lock
```

Commit the new migration and the updated `scripts/migrations.lock` together.

When building a schema release, name the release's migration files explicitly:

```bash
python3 scripts/build-release.py build --version 1.2.12 \
  --migration migrate-017-discovery-v2.sql \
  --migration migrate-018-discovery-windows-creds.sql
```

`--include-migrations` no longer packages every historical migration. It now
requires explicit `--migration` values so an edited old file cannot block an
unrelated appliance update.

The release builder excludes SQL migrations from the ordinary `code/scripts/`
payload. Selected migrations are shipped under the package `migrations/`
directory, executed from there, and then copied into `/opt/zenplus/scripts/`
after successful application.

## Checksum Drift Recovery

If an appliance reports a message such as:

```text
changed migrate-004-snmp.sql checksum differs from applied record
```

the appliance has already applied a file with that name, but the update package
contains different bytes for the same filename. Treat this as a release problem,
not as a database problem.

Preferred recovery:

1. Rebuild the release without historical migrations.
2. Include only the new migration files required for the target version.
3. If the schema needs a correction, create a new forward-only migration.

For a single blocked appliance, only after a database backup and schema
inspection, an operator may update `schema_migrations.checksum` to match the
currently shipped file if the database already reflects the intended schema.
Do not delete rows from `schema_migrations` or rerun old migrations blindly.

## Verification

The runner has unit coverage for:

- sorted migration discovery
- ClickHouse file exclusion
- optional init/seed inclusion
- skip behavior for already-applied migrations
- checksum drift detection
