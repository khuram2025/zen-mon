"""Run database migrations (PostgreSQL and ClickHouse)."""

import logging
import os
import shutil
import subprocess

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


@step_handler("run_migration")
def run_migration(step: dict, extract_dir: str, cfg) -> None:
    engine = step.get("engine", "postgres")
    sql_file = step.get("file", "")
    sql_path = os.path.join(extract_dir, sql_file)

    if not os.path.exists(sql_path):
        raise FileNotFoundError(f"Migration file not found: {sql_path}")

    sql = open(sql_path).read()
    logger.info("Running %s migration: %s", engine, sql_file)

    if engine == "postgres":
        _run_postgres(sql, sql_path)
    elif engine == "clickhouse":
        _run_clickhouse(sql, sql_path)
    else:
        raise ValueError(f"Unknown database engine: {engine}")


def _run_postgres(sql: str, sql_path: str) -> None:
    """Execute SQL against PostgreSQL."""
    runner = "/opt/zenplus/scripts/run-migrations.py"
    scripts_dir = os.path.dirname(sql_path)
    if os.path.exists(runner) and shutil.which("python3"):
        result = subprocess.run(
            [
                "sudo", "-u", "postgres", "python3", runner,
                "--scripts-dir", scripts_dir,
                "--database", "zenplus",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PostgreSQL migration failed: {result.stderr}")
        logger.info("PostgreSQL migration runner output: %s", result.stdout.strip()[:500])
        return

    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "zenplus", "-f", sql_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PostgreSQL migration failed: {result.stderr}")
    logger.info("PostgreSQL migration output: %s", result.stdout.strip()[:500])


def _run_clickhouse(sql: str, sql_path: str) -> None:
    """Execute a packaged ClickHouse migration file."""
    output = apply_clickhouse_sql(sql)
    logger.info("ClickHouse migration output: %s", output.strip()[:500])


def apply_clickhouse_sql(sql: str, *, timeout: int = 120) -> str:
    """Execute SQL against ClickHouse and return its stdout.

    On appliances ClickHouse runs as the `zenplus-clickhouse` Docker container,
    so there is no clickhouse-client on the host PATH. Use a host binary when
    one exists, otherwise exec the client inside the container — matching how
    install.sh applies ClickHouse schema. SQL is piped on stdin because the
    host migration path is not visible inside the container.

    Shared by the packaged-migration step handler and the post-update
    migration sync (see updater/clickhouse_sync.py) so both talk to ClickHouse
    exactly the same way. Raises RuntimeError on a non-zero exit.
    """
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
        cmd,
        input=sql,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ClickHouse query failed: {result.stderr}")
    return result.stdout
