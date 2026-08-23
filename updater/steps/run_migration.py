"""Run database migrations (PostgreSQL and ClickHouse).

This step exists for manifests that name individual migration files. It is no
longer the mechanism that keeps schema and code in step — ``scripts/sync-schema.py``
runs on every update and converges everything on disk. Both paths share the same
runners, so a release that names a migration and one that does not end up in the
same place.
"""

import importlib.util
import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

ZENPLUS_SCRIPTS = Path("/opt/zenplus/scripts")


@step_handler("run_migration")
def run_migration(step: dict, extract_dir: str, cfg) -> None:
    engine = step.get("engine", "postgres")
    sql_file = step.get("file", "")
    sql_path = os.path.join(extract_dir, sql_file)

    if not os.path.exists(sql_path):
        raise FileNotFoundError(f"Migration file not found: {sql_path}")

    logger.info("Running %s migration: %s", engine, sql_file)

    if engine == "postgres":
        _run_postgres(sql_path)
    elif engine == "clickhouse":
        _run_clickhouse(sql_path)
    else:
        raise ValueError(f"Unknown database engine: {engine}")


def _scripts_dir(sql_path: str) -> str:
    """Prefer the appliance's own migration set over the release payload.

    A payload's ``migrations/`` directory holds only the files one release
    introduced. Running the ledger against it would apply those and leave every
    migration an appliance previously missed invisible — which is exactly how
    appliances drifted apart. ``apply_code`` has already landed the full current
    set in /opt/zenplus/scripts by the time this step runs.
    """
    if ZENPLUS_SCRIPTS.is_dir() and any(ZENPLUS_SCRIPTS.glob("migrate-*.sql")):
        return str(ZENPLUS_SCRIPTS)
    return os.path.dirname(sql_path)


def _run_postgres(sql_path: str) -> None:
    """Execute pending PostgreSQL migrations through the tracked runner."""
    runner = ZENPLUS_SCRIPTS / "run-migrations.py"
    scripts_dir = _scripts_dir(sql_path)
    if runner.exists() and shutil.which("python3"):
        result = subprocess.run(
            [
                "sudo", "-u", "postgres", "python3", str(runner),
                "--scripts-dir", scripts_dir,
                "--database", "zenplus",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PostgreSQL migration failed: {result.stderr}")
        logger.info("PostgreSQL migration runner output: %s", result.stdout.strip()[:500])
        return

    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "zenplus", "-f", sql_path],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PostgreSQL migration failed: {result.stderr}")
    logger.info("PostgreSQL migration output: %s", result.stdout.strip()[:500])


def _run_clickhouse(sql_path: str) -> None:
    """Execute a packaged ClickHouse migration file."""
    output = apply_clickhouse_sql(Path(sql_path).read_text())
    logger.info("ClickHouse migration output: %s", output.strip()[:500])


def _load_ch_migrate():
    """Load scripts/ch_migrate.py, the shared ClickHouse client."""
    path = ZENPLUS_SCRIPTS / "ch_migrate.py"
    spec = importlib.util.spec_from_file_location("zenplus_ch_migrate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_clickhouse_sql(sql: str, *, timeout: int = 120) -> str:
    """Execute SQL against ClickHouse and return its stdout.

    Delegates to ``scripts/ch_migrate.ch_query`` so there is one definition of
    how this appliance talks to ClickHouse. The inline fallback covers the
    window on an appliance that has not yet received ch_migrate.py.
    """
    try:
        return _load_ch_migrate().ch_query(sql, timeout=timeout)
    except (ImportError, OSError, SyntaxError, AttributeError):
        pass

    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    host_client = shutil.which("clickhouse-client")
    if host_client:
        cmd = [host_client, "--host", "127.0.0.1", "--port", "9000", "--multiquery"]
    else:
        cmd = ["docker", "exec", "-i", "zenplus-clickhouse",
               "clickhouse-client", "--multiquery"]
    if password:
        cmd += ["--password", password]

    result = subprocess.run(
        cmd, input=sql, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"ClickHouse query failed: {result.stderr}")
    return result.stdout
