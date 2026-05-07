#!/usr/bin/env python3
"""Run ZenPlus SQL migrations with applied-migration tracking.

PostgreSQL migrations are tracked in ``schema_migrations``. ClickHouse
migrations are intentionally left to the existing installer path for now.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


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


def discover_migrations(scripts_dir: Path, include_init: bool = False) -> list[Migration]:
    files: list[Path] = []
    if include_init:
        for name in ("init-postgres.sql", "seed-devices.sql"):
            path = scripts_dir / name
            if path.exists():
                files.append(path)

    files.extend(
        path
        for path in sorted(scripts_dir.glob("migrate-*.sql"))
        if "clickhouse" not in path.name
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


def run_migrations(
    migrations: list[Migration],
    cmd: list[str],
    *,
    dry_run: bool = False,
    status_only: bool = False,
) -> int:
    ensure_tracking(cmd)
    applied = applied_migrations(cmd)

    exit_code = 0
    for migration in migrations:
        existing_checksum = applied.get(migration.filename)
        if existing_checksum == migration.checksum:
            print(f"skip    {migration.filename}")
            continue
        if existing_checksum and existing_checksum != migration.checksum:
            print(f"changed {migration.filename} checksum differs from applied record", file=sys.stderr)
            exit_code = 2
            continue
        if status_only or dry_run:
            print(f"pending {migration.filename}")
            continue

        print(f"apply   {migration.filename}")
        start = time.monotonic()
        result = run_psql(cmd, file=migration.path)
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            raise RuntimeError(f"Migration failed: {migration.filename}")
        record_migration(cmd, migration, duration_ms)

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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    scripts_dir = Path(args.scripts_dir)
    migrations = discover_migrations(scripts_dir, include_init=args.include_init)
    cmd = psql_base_cmd(args.database, args.user, args.host, args.port)
    return run_migrations(migrations, cmd, dry_run=args.dry_run, status_only=args.status)


if __name__ == "__main__":
    raise SystemExit(main())
