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


def test_select_migrations_without_names_selects_every_migration(tmp_path):
    """--include-migrations with no explicit names now means "all of them".

    It used to be a hard error, on the theory that packaging historical
    migrations was unsafe. It is not: the runners keep a ledger and apply only
    what is missing, and requiring a hand-written list is what let a release
    ship code whose schema change never reached appliances.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-002-b.sql", "SELECT 2;")
    write(scripts / "migrate-001-a.sql", "SELECT 1;")

    selected = build_release._select_migrations(scripts, True, [])

    assert [p.name for p in selected] == ["migrate-001-a.sql", "migrate-002-b.sql"]


def test_select_migrations_defaults_to_none_without_the_flag(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")

    assert build_release._select_migrations(scripts, False, []) == []


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


# ─── Guards on newly added migrations ─────────────────────────────────────────


def test_lint_rejects_a_duplicate_sequence_number(tmp_path, capsys):
    """A reused number makes the relative order of two migrations arbitrary."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    existing = write(scripts / "migrate-030-server-monitoring.sql", "SELECT 1;")
    write(scripts / "migrate-030-something-else.sql", "SELECT 2;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  {existing.name}\n")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(
            update_lock=True, scripts_dir=scripts, lock_path=lock
        )

    assert exc.value.code == 1
    assert "sequence 030 is already taken" in capsys.readouterr().out


def test_lint_rejects_a_non_replayable_clickhouse_migration(tmp_path, capsys):
    """The updater heals a false ledger entry by re-running the file. A
    migration that backfills with a blind INSERT cannot be healed that way, so
    it must never ship."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(
        scripts / "migrate-070-bad-clickhouse.sql",
        "CREATE TABLE IF NOT EXISTS zenplus.t (a Int) ENGINE = Log;\n"
        "INSERT INTO zenplus.t SELECT 1;\n",
    )
    lock = tmp_path / "migrations.lock"
    lock.write_text("")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(
            update_lock=True, scripts_dir=scripts, lock_path=lock
        )

    assert exc.value.code == 1
    assert "replay-safe" in capsys.readouterr().out


def test_lint_accepts_a_well_formed_new_clickhouse_migration(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(
        scripts / "migrate-070-good-clickhouse.sql",
        "CREATE TABLE IF NOT EXISTS zenplus.t (a Int) ENGINE = Log;\n",
    )
    lock = tmp_path / "migrations.lock"
    lock.write_text("")

    build_release.lint_migrations(update_lock=True, scripts_dir=scripts, lock_path=lock)

    assert "migrate-070-good-clickhouse.sql" in lock.read_text()


def test_lint_rejects_deleting_a_shipped_migration(tmp_path, capsys):
    """Appliances that never applied it would be stranded with no way to catch up."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-gone.sql\n")

    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)

    assert exc.value.code == 1
    assert "missing from disk" in capsys.readouterr().out


def test_updating_the_lock_never_reorders_existing_entries(tmp_path):
    """Lockfile line order is the sequence appliances apply migrations in."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-030-b.sql", "SELECT 1;")
    write(scripts / "migrate-002-a.sql", "SELECT 2;")
    write(scripts / "migrate-031-new.sql", "SELECT 3;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(
        f"{hash_of('SELECT 1;')}  migrate-030-b.sql\n"
        f"{hash_of('SELECT 2;')}  migrate-002-a.sql\n"
    )

    build_release.lint_migrations(update_lock=True, scripts_dir=scripts, lock_path=lock)

    names = [line.split()[1] for line in lock.read_text().splitlines() if line.strip()]
    assert names == ["migrate-030-b.sql", "migrate-002-a.sql", "migrate-031-new.sql"]


# ─── Git-history audit ────────────────────────────────────────────────────────


def git_repo(tmp_path: Path) -> Path:
    """A throwaway checkout with a scripts/ directory, ready to commit into."""
    import subprocess

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    return scripts


def git_commit(scripts: Path, message: str) -> None:
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=scripts.parent, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=scripts.parent, check=True, capture_output=True,
    )


def test_history_audit_catches_a_migration_edited_after_it_shipped(tmp_path, capsys):
    """The 004/006 failure, reproduced: an edit the lockfile never witnessed.

    Both files were rewritten before migrations.lock existed, so the lock
    recorded the post-rewrite hash as if it were original and the lint passed
    for three months while part of the fleet carried the old one.
    """
    scripts = git_repo(tmp_path)
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    git_commit(scripts, "ship it")
    write(scripts / "migrate-001-a.sql", "SELECT 1; -- rewritten in place")
    git_commit(scripts, "edit it")

    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1; -- rewritten in place')}  migrate-001-a.sql\n")

    # Lock and disk agree, so the original drift check is happy.
    with pytest.raises(SystemExit) as exc:
        build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "edited after it shipped" in out
    assert hash_of("SELECT 1;") in out


def test_history_audit_accepts_a_reconciled_checksum(tmp_path, monkeypatch):
    """Once the superseded hash is recorded, the runner heals the ledger row and
    the build is allowed through."""
    scripts = git_repo(tmp_path)
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    git_commit(scripts, "ship it")
    write(scripts / "migrate-001-a.sql", "SELECT 1; -- rewritten in place")
    git_commit(scripts, "edit it")

    monkeypatch.setattr(
        build_release,
        "_superseded_checksums",
        lambda: {"migrate-001-a.sql": {hash_of("SELECT 1;")}},
    )
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1; -- rewritten in place')}  migrate-001-a.sql\n")

    build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)


def test_history_audit_ignores_clickhouse_migrations(tmp_path):
    """ch_migrate.py keys its ledger on filename and object presence, never on
    a checksum, so a rewritten ClickHouse migration strands nobody."""
    scripts = git_repo(tmp_path)
    write(scripts / "migrate-001-a-clickhouse.sql", "CREATE TABLE IF NOT EXISTS zenplus.t (a Int) ENGINE = Log;")
    git_commit(scripts, "ship it")
    write(scripts / "migrate-001-a-clickhouse.sql", "CREATE TABLE IF NOT EXISTS zenplus.t (a Int, b Int) ENGINE = Log;")
    git_commit(scripts, "edit it")

    lock = tmp_path / "migrations.lock"
    lock.write_text(
        f"{hash_of('CREATE TABLE IF NOT EXISTS zenplus.t (a Int, b Int) ENGINE = Log;')}"
        "  migrate-001-a-clickhouse.sql\n"
    )

    build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)


def test_history_audit_is_silent_outside_a_git_checkout(tmp_path):
    """Building from an extracted payload or a source tarball must still work."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write(scripts / "migrate-001-a.sql", "SELECT 1;")
    lock = tmp_path / "migrations.lock"
    lock.write_text(f"{hash_of('SELECT 1;')}  migrate-001-a.sql\n")

    assert build_release._lint_migration_history(scripts, {"migrate-001-a.sql": hash_of("SELECT 1;")}) == []
    build_release.lint_migrations(scripts_dir=scripts, lock_path=lock)


def test_repo_history_holds_no_unreconciled_migration_edits():
    """The whole fleet's exposure, checked against the whole repo history.

    Three migrations have ever been rewritten. 004-snmp and 006-services-v2 are
    reconciled in run-migrations.py; 004-snmp-clickhouse is not checksum-gated.
    A fourth would strand appliances, and must be caught here rather than by a
    customer's failed update.
    """
    repo_scripts = Path(__file__).resolve().parents[2] / "scripts"
    on_disk = {
        f.name: build_release.sha256_file(str(f))
        for f in sorted(repo_scripts.glob("migrate-*.sql"))
    }

    assert build_release._lint_migration_history(repo_scripts, on_disk) == []


def test_superseded_checksums_are_loaded_from_the_runner():
    """The audit must read the runner's table, not a copy of it."""
    superseded = build_release._superseded_checksums()

    assert "migrate-004-snmp.sql" in superseded
    assert "migrate-006-services-v2.sql" in superseded


def test_migrations_are_shipped_in_the_code_payload():
    """Every release must carry the complete migration set; an appliance that
    skipped a release has no other way to receive what it missed."""
    assert "migrate-*.sql" not in build_release.SCRIPT_CODE_IGNORE
    assert "migrations.lock" not in build_release.SCRIPT_CODE_IGNORE
