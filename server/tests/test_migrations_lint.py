from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BUILD_RELEASE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build-release.py"
spec = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_PATH)
build_release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["build_release"] = build_release
spec.loader.exec_module(build_release)


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def hash_of(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()


def test_lint_passes_when_all_migrations_match_lockfile(tmp_path, capsys):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    write(scripts / "migrate-002-b.sql", "SELECT 2;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(
        f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n"
        f"{hash_of('SELECT 2;')}  migrate-002-b.sql\n"
    )

    build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)


def test_lint_fails_on_drift(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1; -- edited after release")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)
    assert exc.value.code == 1

    # Lockfile must be unchanged after a failed lint.
    assert lock.read_text() == f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n"


def test_lint_fails_when_new_migration_is_not_locked(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    write(scripts / "migrate-002-new.sql", "SELECT 2;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)
    assert exc.value.code == 1


def test_lint_records_new_migrations_when_update_lock_is_set(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    write(scripts / "migrate-002-new.sql", "SELECT 2;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n")

    build_release.lint_migrations(update_lock=True, scripts_dir=scripts, lock_path=lock)

    expected = (
        f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n"
        f"{hash_of('SELECT 2;')}  migrate-002-new.sql\n"
    )
    assert lock.read_text() == expected


def test_lint_update_lock_does_not_silence_drift(tmp_path):
    """--update-lock is a way to bless NEW files, not to overwrite drift."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1; -- edited")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(update_lock=True, scripts_dir=scripts, lock_path=lock)
    assert exc.value.code == 1


def test_lint_passes_with_empty_lockfile_only_if_no_migrations(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    lock = tmp_path / "migrations.lock"  # does not exist

    build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)


def test_select_migrations_requires_explicit_files_when_enabled(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")

    with pytest.raises(SystemExit) as exc:
        build_release._select_migrations(scripts, True, [])

    assert exc.value.code == 1


def test_select_migrations_accepts_names_and_scripts_paths(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    first = write(scripts / "migrate-018-b.sql", "SELECT 18;")
    second = write(scripts / "migrate-017-a.sql", "SELECT 17;")

    selected = build_release._select_migrations(
        scripts,
        False,
        ["scripts/migrate-018-b.sql", "migrate-017-a.sql"],
    )

    assert selected == [second, first]


def test_select_migrations_rejects_duplicate_or_non_migration_files(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    write(scripts / "notes.sql", "SELECT 2;")

    with pytest.raises(SystemExit) as duplicate:
        build_release._select_migrations(
            scripts,
            False,
            ["migrate-001-a.sql", "scripts/migrate-001-a.sql"],
        )
    assert duplicate.value.code == 1

    with pytest.raises(SystemExit) as invalid:
        build_release._select_migrations(scripts, False, ["notes.sql"])
    assert invalid.value.code == 1


def test_migration_engine_routes_clickhouse_suffixes():
    assert build_release._migration_engine(Path("migrate-004-snmp.sql")) == "postgres"
    assert build_release._migration_engine(Path("migrate-004-snmp-clickhouse.sql")) == "clickhouse"
    assert build_release._migration_engine(Path("ch-001-init.sql")) == "clickhouse"


def test_repo_lockfile_matches_current_migrations(tmp_path):
    """Sanity check: the lockfile committed to the repo matches the migrations
    actually in scripts/. If this fails, either edit-then-release was attempted
    (forbidden) or a new migration was added without running
    `lint-migrations --update-lock`.
    """
    repo_scripts = Path(__file__).resolve().parents[2] / "scripts"
    repo_lock = repo_scripts / "migrations.lock"
    build_release.lint_migrations(scripts_dir=repo_scripts, lock_path=repo_lock)
