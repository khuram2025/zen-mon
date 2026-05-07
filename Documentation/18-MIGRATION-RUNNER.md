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

## Updater Integration

The OTA updater `run_migration` step now prefers the tracked migration runner for PostgreSQL migrations when `/opt/zenplus/scripts/run-migrations.py` is available. It falls back to the legacy direct `psql -f` path only if the runner is unavailable.

## Verification

The runner has unit coverage for:

- sorted migration discovery
- ClickHouse file exclusion
- optional init/seed inclusion
- skip behavior for already-applied migrations
- checksum drift detection

