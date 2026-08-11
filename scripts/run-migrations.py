#!/usr/bin/env python3
"""Run ZenPlus SQL migrations with applied-migration tracking.

PostgreSQL migrations are tracked in ``schema_migrations``. ClickHouse
migrations are handled by ``updater/clickhouse_sync.py``, which keeps an
equivalent ledger in ClickHouse.

Migrations are applied in release order (see ``migration_order``), never in
plain filename order — migration numbers are not unique, so a filename sort is
not a stable sequence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_order  # noqa: E402


DEFAULT_DB = "zenplus"
TRACKING_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    path: Path
    filename: str
    checksum: str


# Migrations whose text was edited after they had already shipped and run in
# the field, mapped to the checksums that edit superseded. An appliance
# carrying one of these recorded a legitimately-applied migration; it is not
# drift, and failing the update over it strands the appliance on its current
# version.
#
# Only add an entry when the rewrite left nothing for an already-migrated
# appliance to do. These must be reconciled by recording the new checksum,
# never by re-running the file: every entry below rewrites
# alert_rules_metric_check, which migrations 022/037/039/060 have since
# rewritten again, so re-running would drop the current constraint and
# replace it with a much older, narrower metric list.
SUPERSEDED_CHECKSUMS: dict[str, set[str]] = {
    # 3fb2edc (2026-05-14) widened both CHECK lists into supersets so the
    # drop+add became order-independent. Appliances that had already run
    # either migration keep the constraint a later migration gave them.
    "migrate-004-snmp.sql": {
        "d1a31a15bdf5539c798b4822ccf2eeebb1d0ba2d5b8bf348ca801410e74b0652",
    },
    "migrate-006-services-v2.sql": {
        "12acd2f87031ae6e7ab7e78ab6e49e67b6b3663516acc8da0bf03eaed04dca73",
    },
}


def is_superseded(filename: str, applied_checksum: str) -> bool:
    """Whether a differing checksum is a known historical rewrite."""
    return applied_checksum in SUPERSEDED_CHECKSUMS.get(filename, frozenset())


def discover_migrations(
    scripts_dir: Path,
    include_init: bool = False,
    *,
    lock_path: Path | None = None,
) -> list[Migration]:
    """Return PostgreSQL migrations under scripts_dir in release order."""
    files: list[Path] = []
    if include_init:
        for name in ("init-postgres.sql", "seed-devices.sql"):
            path = scripts_dir / name
            if path.exists():
                files.append(path)

    files.extend(
        migration_order.ordered_migrations(
            scripts_dir, engine="postgres", lock_path=lock_path
        )
    )
    return [Migration(path=p, filename=p.name, checksum=sha256_file(p)) for p in files]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def psql_base_cmd(database: str, user: str | None, host: str | None, port: str | None) -> list[str]:
    cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-d", database]
    if user:
        cmd.extend(["-U", user])
    if host:
        cmd.extend(["-h", host])
    if port:
        cmd.extend(["-p", port])
    return cmd


def run_psql(cmd: list[str], *, sql: str | None = None, file: Path | None = None) -> subprocess.CompletedProcess:
    full_cmd = list(cmd)
    if file is not None:
        full_cmd.extend(["-f", str(file)])
    return subprocess.run(
        full_cmd,
        input=sql,
        text=True,
        capture_output=True,
        timeout=300,
    )


def ensure_tracking(cmd: list[str]) -> None:
    result = run_psql(cmd, sql=TRACKING_SQL)
    if result.returncode != 0:
        raise RuntimeError(f"Could not create schema_migrations: {result.stderr.strip()}")


def applied_migrations(cmd: list[str]) -> dict[str, str]:
    result = run_psql(
        cmd,
        sql="SELECT filename || ' ' || checksum FROM schema_migrations ORDER BY filename;",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read schema_migrations: {result.stderr.strip()}")

    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("(") or line.lower().startswith("filename"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(".sql"):
            out[parts[0]] = parts[1]
    return out


# Relations, columns, constraints and indexes in one round trip. Columns and
# constraints matter because several migrations only widen an existing table —
# they CREATE nothing, so without these a correct database still looks pending
# and the migration gets re-applied.
EXISTING_OBJECTS_SQL = """
SELECT n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
UNION ALL
SELECT 'column:' || c.table_name || '.' || c.column_name
FROM information_schema.columns c
WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
UNION ALL
SELECT 'constraint:' || con.conname
FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
UNION ALL
SELECT 'index:' || i.indexname
FROM pg_indexes i
WHERE i.schemaname NOT IN ('pg_catalog', 'information_schema');
"""


def existing_objects(cmd: list[str]) -> set[str]:
    """Return every relation in the database, qualified and bare, lowercased.

    Migrations name tables unqualified (``devices``), so both forms are stored
    and the caller can match either.
    """
    result = run_psql(cmd, sql=EXISTING_OBJECTS_SQL)
    if result.returncode != 0:
        raise RuntimeError(f"Could not list relations: {result.stderr.strip()}")

    objects: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip().lower()
        if not line or line.startswith("(") or line.startswith("-"):
            continue
        # Tagged entries (column:/constraint:/index:) are stored verbatim; only
        # bare relation names get the extra unqualified alias.
        if line.startswith(("column:", "constraint:", "index:")):
            objects.add(line)
            continue
        if "." not in line:
            continue
        objects.add(line)
        objects.add(line.split(".", 1)[1])
    return objects


def evidence_of(migration: Migration, present: set[str], sql: str | None = None) -> bool:
    """True when this migration's tables already exist in the database.

    An appliance installed before migrations were tracked has a full schema and
    an empty ledger — every migration would look pending. Re-running them would
    duplicate seed rows from the seven migrations that INSERT. Asking the
    database what it already has turns an unknowable "has this run?" into an
    answerable one.
    """
    text = migration.path.read_text() if sql is None else sql
    objects = migration_order.created_objects(text)
    columns = migration_order.added_columns(text)
    indexes = migration_order.created_indexes(text)

    # Constraints are deliberately NOT used as evidence. Nine migrations drop
    # and recreate alert_rules_metric_check under the same name, each widening
    # the allowed list, so its presence says nothing about which of them ran —
    # and a migration that failed midway leaves it dropped entirely, since these
    # files carry no transaction wrapper. Tables, columns and indexes are
    # additive and stable, so they are the only trustworthy evidence.
    if not (objects or columns or indexes):
        return False
    return (
        all(obj.lower() in present for obj in objects)
        and all(f"column:{col}" in present for col in columns)
        and all(f"index:{idx}" in present for idx in indexes)
    )


def record_migration(cmd: list[str], migration: Migration, duration_ms: int) -> None:
    sql = (
        "INSERT INTO schema_migrations (filename, checksum, duration_ms) "
        f"VALUES ('{escape_sql(migration.filename)}', '{migration.checksum}', {duration_ms}) "
        "ON CONFLICT (filename) DO UPDATE SET "
        "checksum = EXCLUDED.checksum, applied_at = NOW(), duration_ms = EXCLUDED.duration_ms;"
    )
    result = run_psql(cmd, sql=sql)
    if result.returncode != 0:
        raise RuntimeError(f"Could not record {migration.filename}: {result.stderr.strip()}")


def escape_sql(value: str) -> str:
    return value.replace("'", "''")


def new_report() -> dict:
    """Empty structured result, filled in by run_migrations()."""
    return {"applied": [], "skipped": [], "pending": [], "drift": [],
            "failed": [], "baselined": [], "unresolved": [], "reconciled": []}


def run_migrations(
    migrations: list[Migration],
    cmd: list[str],
    *,
    dry_run: bool = False,
    status_only: bool = False,
    report: dict | None = None,
) -> int:
    """Apply every pending migration in the given order.

    ``report`` is filled in with the per-file outcome so callers (the OTA
    updater) can gate on real state instead of parsing stdout. Returns 0 when
    the database matches the migration set on disk, 2 when it does not.
    """
    if report is None:
        report = new_report()
    ensure_tracking(cmd)
    applied = applied_migrations(cmd)
    present = existing_objects(cmd)

    exit_code = 0
    for migration in migrations:
        existing_checksum = applied.get(migration.filename)
        if existing_checksum == migration.checksum:
            print(f"skip    {migration.filename}")
            report["skipped"].append(migration.filename)
            continue
        if existing_checksum and existing_checksum != migration.checksum:
            if is_superseded(migration.filename, existing_checksum):
                # Applied before the file was rewritten. Heal the record so
                # this appliance stops tripping the gate on every update.
                print(f"reconcile {migration.filename} (applied before a known rewrite)")
                report["reconciled"].append(migration.filename)
                if not (status_only or dry_run):
                    record_migration(cmd, migration, 0)
                continue
            print(f"changed {migration.filename} checksum differs from applied record", file=sys.stderr)
            report["drift"].append(migration.filename)
            exit_code = 2
            continue

        sql = migration.path.read_text()
        if not existing_checksum and evidence_of(migration, present, sql):
            # Its tables are already there: record it instead of re-running it.
            print(f"baseline {migration.filename} (objects already present)")
            report["baselined"].append(migration.filename)
            if not (status_only or dry_run):
                record_migration(cmd, migration, 0)
            continue

        if not existing_checksum and migration_order.writes_rows(sql) \
                and not migration_order.created_objects(sql):
            # Nothing to probe and it INSERTs rows — running it blind could
            # duplicate data. Surface it rather than guess.
            print(f"unresolved {migration.filename}: cannot verify, inserts rows",
                  file=sys.stderr)
            report["unresolved"].append(migration.filename)
            exit_code = 2
            continue

        if status_only or dry_run:
            print(f"pending {migration.filename}")
            report["pending"].append(migration.filename)
            exit_code = 2
            continue

        print(f"apply   {migration.filename}")
        start = time.monotonic()
        result = run_psql(cmd, file=migration.path)
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            report["failed"].append(
                {"filename": migration.filename, "error": result.stderr.strip()[:2000]}
            )
            raise RuntimeError(f"Migration failed: {migration.filename}")
        record_migration(cmd, migration, duration_ms)
        report["applied"].append(migration.filename)
        present |= {o.lower() for o in migration_order.created_objects(sql)}

    return exit_code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ZenPlus PostgreSQL migrations")
    parser.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", DEFAULT_DB))
    parser.add_argument("--user", default=os.getenv("PGUSER"))
    parser.add_argument("--host", default=os.getenv("PGHOST"))
    parser.add_argument("--port", default=os.getenv("PGPORT"))
    parser.add_argument("--include-init", action="store_true", help="Include init-postgres.sql and seed-devices.sql")
    parser.add_argument("--dry-run", action="store_true", help="Print pending migrations without applying them")
    parser.add_argument("--status", action="store_true", help="Show applied/pending status without applying migrations")
    parser.add_argument("--lock", default=None, help="Path to migrations.lock (defaults to one in --scripts-dir)")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable summary on the last stdout line")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    scripts_dir = Path(args.scripts_dir)
    lock_path = Path(args.lock) if args.lock else None
    migrations = discover_migrations(
        scripts_dir, include_init=args.include_init, lock_path=lock_path
    )
    cmd = psql_base_cmd(args.database, args.user, args.host, args.port)
    report = new_report()
    try:
        code = run_migrations(
            migrations,
            cmd,
            dry_run=args.dry_run,
            status_only=args.status,
            report=report,
        )
    except RuntimeError as e:
        report["error"] = str(e)
        code = 1
        if not args.json:
            raise
    if args.json:
        print("ZENPLUS_MIGRATION_JSON " + json.dumps(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
