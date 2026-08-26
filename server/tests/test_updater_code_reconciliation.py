from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from updater import code_inventory, rollback  # noqa: E402
from updater.steps import apply_code  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_apply_replace_removes_stale_code_and_preserves_runtime_state(
    monkeypatch, tmp_path,
):
    appliance = tmp_path / "appliance"
    code = tmp_path / "release" / "code"

    _write(code / "server" / "current.py", "current")
    _write(code / "updater" / "steps" / "current.py", "current")
    code_inventory.write_payload_manifest(
        code,
        managed_roots=("server", "updater"),
        managed_files=(),
    )

    _write(appliance / "server" / "stale.py", "must disappear")
    _write(appliance / "server" / "venv" / "site.py", "runtime")
    _write(appliance / "updater" / "steps" / "stale.py", "must disappear")
    _write(appliance / "updater" / "config" / "agent.conf", "secret")
    _write(appliance / ".env", "appliance-secret")
    monkeypatch.setattr(apply_code, "ZENPLUS_DIR", appliance)

    apply_code._apply_replace(
        {"source": "code/"}, str(code.parent),
    )

    assert (appliance / "server" / "current.py").read_text() == "current"
    assert not (appliance / "server" / "stale.py").exists()
    assert (appliance / "server" / "venv" / "site.py").read_text() == "runtime"
    assert not (appliance / "updater" / "steps" / "stale.py").exists()
    assert (appliance / "updater" / "config" / "agent.conf").read_text() == "secret"
    assert (appliance / ".env").read_text() == "appliance-secret"


def test_payload_inventory_mismatch_aborts_before_deleting(monkeypatch, tmp_path):
    appliance = tmp_path / "appliance"
    code = tmp_path / "release" / "code"
    _write(code / "server" / "current.py", "current")
    code_inventory.write_payload_manifest(code, managed_roots=("server",))
    _write(code / "server" / "not-in-signed-inventory.py", "unexpected")
    _write(appliance / "server" / "existing.py", "untouched")
    monkeypatch.setattr(apply_code, "ZENPLUS_DIR", appliance)

    with pytest.raises(ValueError, match="does not match payload"):
        apply_code._apply_replace({"source": "code/"}, str(code.parent))

    assert (appliance / "server" / "existing.py").read_text() == "untouched"


def test_backup_restore_is_exact_for_code_and_preserves_local_state(
    monkeypatch, tmp_path,
):
    appliance = tmp_path / "appliance"
    backup_base = appliance / "updater" / "backups"
    monkeypatch.setattr(rollback, "ZENPLUS_DIR", appliance)

    _write(appliance / "server" / "old.py", "old-server")
    _write(appliance / "updater" / "agent.py", "old-updater")
    _write(appliance / "support" / "collector.py", "old-support")
    _write(appliance / "docker" / "clickhouse.xml", "old-docker")
    _write(appliance / "dashboard" / "dist" / "index.html", "old-dashboard")
    _write(appliance / "docker-compose.yml", "old-compose")
    _write(appliance / ".version", "1.0.0")
    _write(appliance / "updater" / "config" / "agent.conf", "old-secret")

    created = Path(rollback.create_backup(
        str(backup_base), "1.0.0", include_db=False,
    ))
    assert (created / rollback.BACKUP_MANIFEST).is_file()

    _write(appliance / "server" / "old.py", "new-server")
    _write(appliance / "server" / "new-only.py", "new-only")
    _write(appliance / "updater" / "agent.py", "new-updater")
    _write(appliance / "updater" / "new-only.py", "new-only")
    _write(appliance / "support" / "new-only.py", "new-only")
    _write(appliance / "docker" / "new-only.xml", "new-only")
    _write(appliance / "dashboard" / "dist" / "index.html", "new-dashboard")
    _write(appliance / "docker-compose.yml", "new-compose")
    # Runtime credentials are allowed to change independently of code rollback.
    _write(appliance / "updater" / "config" / "agent.conf", "rotated-secret")

    rollback.restore_backup(str(backup_base))

    assert (appliance / "server" / "old.py").read_text() == "old-server"
    assert (appliance / "updater" / "agent.py").read_text() == "old-updater"
    assert (appliance / "support" / "collector.py").read_text() == "old-support"
    assert (appliance / "docker" / "clickhouse.xml").read_text() == "old-docker"
    assert (appliance / "dashboard" / "dist" / "index.html").read_text() == "old-dashboard"
    assert (appliance / "docker-compose.yml").read_text() == "old-compose"
    for path in (
        appliance / "server" / "new-only.py",
        appliance / "updater" / "new-only.py",
        appliance / "support" / "new-only.py",
        appliance / "docker" / "new-only.xml",
    ):
        assert not path.exists()
    assert (
        appliance / "updater" / "config" / "agent.conf"
    ).read_text() == "rotated-secret"


def test_legacy_backup_without_inventory_keeps_overlay_restore(monkeypatch, tmp_path):
    appliance = tmp_path / "appliance"
    backup = tmp_path / "backups" / "pre-legacy"
    backup.mkdir(parents=True)
    archived = tmp_path / "archived"
    _write(archived / "server" / "old.py", "old")
    with tarfile.open(backup / "code.tar.gz", "w:gz") as tar:
        tar.add(archived / "server", arcname="server")

    _write(appliance / "server" / "new-only.py", "new")
    monkeypatch.setattr(rollback, "ZENPLUS_DIR", appliance)

    rollback.restore_backup(str(backup.parent))

    assert (appliance / "server" / "old.py").read_text() == "old"
    assert (appliance / "server" / "new-only.py").read_text() == "new"


def test_release_builder_runs_reconciler_for_pre_upgrade_updater_process():
    source = (ROOT / "scripts" / "build-release.py").read_text(encoding="utf-8")
    apply_at = source.index('steps.append({"type": "apply_code"')
    bridge_at = source.index('"script": "code/scripts/reconcile-code-payload.py"')

    assert apply_at < bridge_at


def test_postgres_backup_failure_aborts_update(monkeypatch, tmp_path):
    def fail_to_start(*_args, **_kwargs):
        raise OSError("pg_dump unavailable")

    monkeypatch.setattr(rollback.subprocess, "Popen", fail_to_start)

    with pytest.raises(RuntimeError, match="PostgreSQL backup failed"):
        rollback._backup_postgres(tmp_path)

    assert not (tmp_path / "pg_dump.sql.gz").exists()


def test_postgres_restore_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    dump = tmp_path / "pg_dump.sql.gz"
    dump.write_bytes(b"not-a-database-backup")

    def fail_to_start(*_args, **_kwargs):
        raise OSError("gunzip unavailable")

    monkeypatch.setattr(rollback.subprocess, "Popen", fail_to_start)

    with pytest.raises(RuntimeError, match="PostgreSQL restore failed"):
        rollback._restore_postgres(dump)
