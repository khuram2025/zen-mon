from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run-migrations.py"
spec = importlib.util.spec_from_file_location("run_migrations", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["run_migrations"] = runner
spec.loader.exec_module(runner)


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_discover_migrations_orders_postgres_files_and_excludes_clickhouse(tmp_path):
    write(tmp_path / "migrate-002-b.sql", "SELECT 2;")
    write(tmp_path / "migrate-001-a.sql", "SELECT 1;")
    write(tmp_path / "migrate-003-clickhouse.sql", "SELECT 3;")
    write(tmp_path / "notes.txt", "ignore")

    migrations = runner.discover_migrations(tmp_path)

    assert [m.filename for m in migrations] == ["migrate-001-a.sql", "migrate-002-b.sql"]
    assert all(len(m.checksum) == 64 for m in migrations)


def test_discover_migrations_can_include_init_and_seed(tmp_path):
    write(tmp_path / "seed-devices.sql", "SELECT 'seed';")
    write(tmp_path / "init-postgres.sql", "SELECT 'init';")
    write(tmp_path / "migrate-001-a.sql", "SELECT 1;")

    migrations = runner.discover_migrations(tmp_path, include_init=True)

    assert [m.filename for m in migrations] == [
        "init-postgres.sql",
        "seed-devices.sql",
        "migrate-001-a.sql",
    ]


def test_run_migrations_skips_matching_applied_migration(tmp_path, capsys):
    path = tmp_path / "migrate-001-a.sql"
    write(path, "SELECT 1;")
    migration = runner.discover_migrations(tmp_path)[0]
    calls = []

    def fake_run_psql(cmd, sql=None, file=None):
        calls.append((sql, file))
        if sql and "SELECT filename" in sql:
            return type("Result", (), {"returncode": 0, "stdout": f"{migration.filename} {migration.checksum}\n", "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    original = runner.run_psql
    runner.run_psql = fake_run_psql
    try:
        code = runner.run_migrations([migration], ["psql"])
    finally:
        runner.run_psql = original

    assert code == 0
    assert "skip    migrate-001-a.sql" in capsys.readouterr().out
    assert all(file is None for _, file in calls)


def test_run_migrations_reports_checksum_drift(tmp_path):
    path = tmp_path / "migrate-001-a.sql"
    write(path, "SELECT 1;")
    migration = runner.discover_migrations(tmp_path)[0]

    def fake_run_psql(cmd, sql=None, file=None):
        if sql and "SELECT filename" in sql:
            return type("Result", (), {"returncode": 0, "stdout": f"{migration.filename} different\n", "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    original = runner.run_psql
    runner.run_psql = fake_run_psql
    try:
        code = runner.run_migrations([migration], ["psql"])
    finally:
        runner.run_psql = original

    assert code == 2
