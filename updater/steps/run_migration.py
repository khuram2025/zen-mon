"""Run database migrations (PostgreSQL and ClickHouse)."""

import logging
import os
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
    """Execute SQL against ClickHouse."""
    result = subprocess.run(
        [
            "clickhouse-client",
            "--host", "127.0.0.1",
            "--port", "9000",
            "--multiquery",
            "--queries-file", sql_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ClickHouse migration failed: {result.stderr}")
    logger.info("ClickHouse migration output: %s", result.stdout.strip()[:500])
