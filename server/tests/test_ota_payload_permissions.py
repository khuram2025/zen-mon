"""Regressions for the 1.23.13 service-account startup/rollback failure."""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from updater import executor, rollback
from updater.steps import apply_code


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "posix", reason="POSIX service permissions")
def test_private_checkout_packages_public_code_and_assets(tmp_path):
    builder = load_script("build-release")
    verifier = load_script("verify-ota-release")
    source = tmp_path / "checkout"
    source.mkdir(mode=0o700)
    (source / "permissions.py").write_text("allowed = True\n")
    (source / "permissions.py").chmod(0o600)
    (source / "hook.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "hook.sh").chmod(0o700)
    package = tmp_path / "payload.tar.gz"
    with tarfile.open(package, "w:gz") as tar:
        tar.add(source, arcname="code", filter=builder.public_payload_member)
    with tarfile.open(package) as tar:
        verifier.verify_public_permissions(tar)
        assert tar.getmember("code").mode == 0o755
        assert tar.getmember("code/permissions.py").mode == 0o644
        assert tar.getmember("code/hook.sh").mode == 0o755
    assert (source / "permissions.py").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode,is_dir", [(0o600, False), (0o700, True), (0o666, False), (0o4755, False)])
def test_verifier_rejects_unreadable_or_unsafe_modes(mode, is_dir):
    verifier = load_script("verify-ota-release")
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        member = tarfile.TarInfo("code/server")
        member.mode = mode
        if is_dir:
            member.type = tarfile.DIRTYPE
        tar.addfile(member)
    data.seek(0)
    with tarfile.open(fileobj=data) as tar:
        with pytest.raises(RuntimeError, match="permissions"):
            verifier.verify_public_permissions(tar)


@pytest.mark.skipif(os.name != "posix", reason="POSIX service permissions")
def test_apply_under_private_umask_preserves_credentials(monkeypatch, tmp_path):
    root = tmp_path / "appliance"
    root.mkdir()
    secret = root / "updater/config/agent.conf"
    secret.parent.mkdir(parents=True, mode=0o700)
    secret.write_text("private fixture")
    secret.chmod(0o600)
    code = tmp_path / "payload/code"
    module = code / "server/app/permissions.py"
    module.parent.mkdir(parents=True)
    module.write_text("allowed = True\n")
    module.chmod(0o600)
    monkeypatch.setattr(apply_code, "ZENPLUS_DIR", root)
    old = os.umask(0o077)
    try:
        apply_code._apply_replace({}, str(code.parent))
    finally:
        os.umask(old)
    assert (root / "server/app").stat().st_mode & 0o777 == 0o755
    assert (root / "server/app/permissions.py").stat().st_mode & 0o777 == 0o644
    assert secret.stat().st_mode & 0o777 == 0o600
    assert secret.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform != "linux", reason="Linux ETXTBSY regression")
def test_restore_replaces_running_executable(monkeypatch, tmp_path):
    root = tmp_path / "appliance"
    binary = root / "bin/collector"
    binary.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("sleep"), binary)
    monkeypatch.setattr(rollback, "ZENPLUS_DIR", root)
    backups = tmp_path / "backups"
    rollback.create_backup(str(backups), "previous", include_db=False)
    process = subprocess.Popen([str(binary), "60"])
    try:
        inode = binary.stat().st_ino
        rollback.restore_backup(str(backups))
        assert binary.stat().st_ino != inode
        assert process.poll() is None
        assert subprocess.run([str(binary), "0"]).returncode == 0
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_rollback_error_is_reported_after_remaining_steps(monkeypatch, caplog):
    calls = []
    def step(value, *_args):
        calls.append(value["type"])
        if value["type"] in {"health_check", "restore_backup"}:
            raise executor.ExecutionError("fixture failure")
    monkeypatch.setattr(executor, "execute_step", step)
    monkeypatch.setattr(executor, "_load_step_handlers", lambda: None)
    manifest = {"steps": [{"type": "backup"}, {"type": "health_check"}],
                "rollback_steps": [{"type": "restore_backup"}, {"type": "start_services"}]}
    with pytest.raises(executor.ExecutionError, match="rollback incomplete: restore_backup"):
        executor.execute_manifest(manifest, "", None)
    assert calls[-1] == "start_services"
    assert "Rollback complete" not in caplog.text
