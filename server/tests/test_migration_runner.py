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


def _fake_psql(applied: dict[str, str], relations: list[str], calls: list | None = None):
    """Stand in for psql: a ledger, a relation list, and a record of what ran."""
    def run(cmd, sql=None, file=None):
        if calls is not None:
            calls.append((sql, file))
        if sql and "SELECT filename" in sql:
            out = "".join(f"{k} {v}\n" for k, v in applied.items())
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        if sql and "pg_class" in sql:
            out = "".join(f"public.{r}\n" for r in relations)
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        if sql and sql.startswith("INSERT INTO schema_migrations"):
            name = sql.split("VALUES ('", 1)[1].split("'", 1)[0]
            applied[name] = "recorded"
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return run


def test_migration_is_baselined_when_its_tables_already_exist(tmp_path, capsys):
    """An appliance installed before tracking has a full schema and an empty
    ledger. Re-running a migration that seeds rows would duplicate them."""
    write(
        tmp_path / "migrate-001-a.sql",
        "CREATE TABLE IF NOT EXISTS gateways (id INT);\n"
        "INSERT INTO gateways VALUES (1);\n",
    )
    migration = runner.discover_migrations(tmp_path)[0]
    applied: dict[str, str] = {}
    calls: list = []
    report = runner.new_report()

    original = runner.run_psql
    runner.run_psql = _fake_psql(applied, ["gateways"], calls)
    try:
        runner.run_migrations([migration], ["psql"], report=report)
    finally:
        runner.run_psql = original

    assert report["baselined"] == ["migrate-001-a.sql"]
    assert report["applied"] == []
    assert all(file is None for _, file in calls)  # the .sql itself never ran
    assert applied["migrate-001-a.sql"] == "recorded"


def test_migration_is_applied_when_its_tables_are_missing(tmp_path):
    write(tmp_path / "migrate-001-a.sql", "CREATE TABLE IF NOT EXISTS gateways (id INT);")
    migration = runner.discover_migrations(tmp_path)[0]
    report = runner.new_report()

    original = runner.run_psql
    runner.run_psql = _fake_psql({}, [])
    try:
        runner.run_migrations([migration], ["psql"], report=report)
    finally:
        runner.run_psql = original

    assert report["applied"] == ["migrate-001-a.sql"]
    assert report["baselined"] == []


def test_column_only_migration_is_applied_not_baselined(tmp_path):
    """Nothing to probe, but guarded DDL — running it again is harmless."""
    write(tmp_path / "migrate-002-b.sql", "ALTER TABLE devices ADD COLUMN IF NOT EXISTS x TEXT;")
    migration = runner.discover_migrations(tmp_path)[0]
    report = runner.new_report()

    original = runner.run_psql
    runner.run_psql = _fake_psql({}, ["devices"])
    try:
        runner.run_migrations([migration], ["psql"], report=report)
    finally:
        runner.run_psql = original

    assert report["applied"] == ["migrate-002-b.sql"]


def test_unverifiable_row_inserting_migration_is_flagged_not_guessed(tmp_path):
    """No objects to probe and it INSERTs: running it blind could double the
    rows, skipping it could leave a gap. Report it instead of guessing."""
    write(tmp_path / "migrate-003-c.sql", "INSERT INTO defaults (k) VALUES ('a');")
    migration = runner.discover_migrations(tmp_path)[0]
    report = runner.new_report()

    original = runner.run_psql
    runner.run_psql = _fake_psql({}, ["defaults"])
    try:
        code = runner.run_migrations([migration], ["psql"], report=report)
    finally:
        runner.run_psql = original

    assert code == 2
    assert report["unresolved"] == ["migrate-003-c.sql"]
    assert report["applied"] == []


def test_status_mode_reports_pending_as_a_nonzero_exit(tmp_path):
    """--status is usable as a health probe: 0 means the schema matches."""
    write(tmp_path / "migrate-001-a.sql", "CREATE TABLE IF NOT EXISTS gateways (id INT);")
    migration = runner.discover_migrations(tmp_path)[0]
    report = runner.new_report()

    original = runner.run_psql
    runner.run_psql = _fake_psql({}, [])
    try:
        code = runner.run_migrations([migration], ["psql"], status_only=True, report=report)
    finally:
        runner.run_psql = original

    assert code == 2
    assert report["pending"] == ["migrate-001-a.sql"]


def test_migrations_are_discovered_in_lockfile_order(tmp_path):
    write(tmp_path / "migrate-030-b.sql", "SELECT 1;")
    write(tmp_path / "migrate-002-a.sql", "SELECT 2;")
    (tmp_path / "migrations.lock").write_text(
        "x  migrate-030-b.sql\ny  migrate-002-a.sql\n"
    )

    names = [m.filename for m in runner.discover_migrations(tmp_path)]

    assert names == ["migrate-030-b.sql", "migrate-002-a.sql"]
