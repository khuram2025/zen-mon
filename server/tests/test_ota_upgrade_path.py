from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from integrations.zentryc_ota.upgrade_path import plan_upgrade, UpgradePathError, rollout_allows
from updater.version_policy import validate_transition, validate_manifest_transition, VersionPolicyError
from updater import agent, schema_gate, executor


def release(version, minimum=None):
    return NS(version=version, min_version=minimum, id=version)


def test_required_chain_and_cumulative_skip():
    catalog = [release("1.23.6"), release("1.23.7"), release("1.23.8", "1.23.7"),
               release("1.23.9", "1.23.8"), release("1.23.10", "1.23.9")]
    assert [r.version for r in plan_upgrade("1.23.3", catalog[-1], catalog)] == [
        "1.23.7", "1.23.8", "1.23.9", "1.23.10"]
    assert [r.version for r in plan_upgrade("1.23.8", catalog[-1], catalog)] == ["1.23.9", "1.23.10"]
    assert plan_upgrade("1.23.10", catalog[-1], catalog) == []


@pytest.mark.parametrize("current,target,catalog", [
    ("1.0.0", release("2.0.0", "1.5.0"), []),
    ("1.0.0", release("2.0.0", "2.0.0"), []),
    ("1.0.0", release("2.0.0", "3.0.0"), []),
    ("unknown", release("2.0.0"), []),
    ("1..0", release("2.0.0"), []),
])
def test_unprovable_paths_fail_closed(current, target, catalog):
    with pytest.raises(UpgradePathError):
        plan_upgrade(current, target, catalog)


@pytest.mark.parametrize("stage,pct,group,completed,expected", [
    ("full", 100, None, None, True), ("paused", 100, None, None, False),
    ("aborted", 100, None, None, False), ("full", 0, None, None, False),
    ("full", 100, "canary", None, False), ("full", 100, None, "done", False),
])
def test_rollout_limits(stage, pct, group, completed, expected):
    policy = NS(stage=stage, target_pct=pct, target_group=group, completed_at=completed)
    assert rollout_allows(NS(id="device", rollout_group="stable"), release("2.0.0"), policy) is expected


@pytest.mark.parametrize("current,minimum,target", [
    ("1.0.0", "1.5.0", "2.0.0"), ("unknown", None, "2.0.0"),
    ("2.0.0", None, "2.0.0"), ("3.0.0", None, "2.0.0"),
    ("1.0.0", "junk", "2.0.0"), ("1.0.0", "3.0.0", "2.0.0"),
])
def test_local_transition_rejection(current, minimum, target):
    with pytest.raises(VersionPolicyError):
        validate_transition(dict(version=target, min_version=minimum), current)


def test_signed_prerequisite_cannot_be_omitted_or_differ_from_offer():
    signed = dict(version="2.0.0", min_version="1.5.0")
    validate_manifest_transition(signed, signed, "1.5.0", "2.0.0")
    with pytest.raises(VersionPolicyError, match="prerequisite"):
        validate_manifest_transition(signed, dict(version="2.0.0"), "1.5.0", "2.0.0")
    with pytest.raises(VersionPolicyError, match="versions do not match"):
        validate_manifest_transition(signed, signed, "1.5.0", "2.0.1")


def test_direct_update_cannot_bypass_minimum(monkeypatch):
    monkeypatch.setattr(agent, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(agent, "report_status", Mock())
    download = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(agent, "download_and_extract", download)
    assert agent.run_update(NS(), dict(version="2.0.0", min_version="1.5.0")) is False
    download.assert_not_called()


@pytest.mark.parametrize("stdout,code", [("", 0), ("not JSON", 0), ('{"ok":true}', 1),
                                         ('{"other":true}', 0), ('{"ok":false}', 0)])
def test_schema_requires_explicit_success(tmp_path, monkeypatch, stdout, code):
    script = tmp_path / "sync-schema.py"
    script.touch()
    monkeypatch.setattr(schema_gate, "SYNC_SCRIPT", script)
    monkeypatch.setattr(schema_gate.subprocess, "run", lambda *a, **k: NS(stdout=stdout, stderr="", returncode=code))
    assert schema_gate.sync_and_verify(tmp_path)["ok"] is False


def test_schema_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(schema_gate, "SYNC_SCRIPT", tmp_path / "absent.py")
    assert schema_gate.sync_and_verify(tmp_path)["ok"] is False


def test_preflight_failure_does_not_restore_an_unrelated_old_backup(monkeypatch):
    monkeypatch.setattr(executor, "_load_step_handlers", lambda: None)
    monkeypatch.setattr(executor, "execute_step", Mock(side_effect=executor.ExecutionError("blocked")))
    rollback = Mock()
    monkeypatch.setattr(executor, "_execute_rollback", rollback)
    with pytest.raises(executor.ExecutionError):
        executor.execute_manifest(dict(steps=[dict(type="run_hook")], rollback_steps=[dict(type="restore_backup")]), "/tmp/test", NS())
    rollback.assert_not_called()


def test_apply_code_keeps_installed_version_until_commit(tmp_path, monkeypatch):
    from updater.steps import apply_code
    payload = tmp_path / "extract" / "code"
    payload.mkdir(parents=True)
    (payload / ".version").write_text("2.0.0")
    (payload / "app.py").write_text("# new code")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / ".version").write_text("1.0.0")
    monkeypatch.setattr(apply_code, "ZENPLUS_DIR", installed)
    monkeypatch.setattr(apply_code, "reconcile_code_tree", lambda *a: [])
    apply_code._apply_replace({}, str(payload.parent))
    assert (installed / ".version").read_text() == "1.0.0"
    assert (installed / "app.py").read_text() == "# new code"


def test_next_hop_reloads_new_updater_and_preserves_config(tmp_path, monkeypatch):
    config = NS(appliance=NS(id="test", api_key="test"))
    lock = Mock()
    monkeypatch.setattr(agent, "load_config", lambda *a: config)
    monkeypatch.setattr(agent, "setup_logging", lambda *a: None)
    monkeypatch.setattr(agent, "UpdateLock", lambda: lock)
    monkeypatch.setattr(agent, "checkin", lambda *a: dict(version="1.1.0"))
    monkeypatch.setattr(agent, "get_current_version", Mock(side_effect=["1.0.0", "1.1.0"]))
    monkeypatch.setattr(agent, "run_update", lambda *a: True)
    monkeypatch.setattr(agent, "check_for_update", lambda *a: dict(version="1.2.0"))
    def restart(exe, args):
        lock.release.assert_called()
        assert args == [sys.executable, "-m", "updater", "--config", str((tmp_path / "agent.conf").resolve())]
        raise SystemExit(0)
    monkeypatch.setattr(agent.os, "execv", restart)
    with pytest.raises(SystemExit) as exc:
        agent.main(str(tmp_path / "agent.conf"))
    assert exc.value.code == 0


def test_release_policy_preserves_bridge_floor():
    sys.path.insert(0, str(ROOT / "scripts"))
    from release_policy import validate_policy
    assert validate_policy(ROOT, "1.23.10", None) == "1.23.9"
    with pytest.raises(ValueError, match="bridge floor"):
        validate_policy(ROOT, "1.23.10", "1.23.7")


def test_failure_stops_before_checking_for_another_hop(monkeypatch):
    cfg = NS(appliance=NS(id="test", api_key="test"))
    monkeypatch.setattr(agent, "load_config", lambda *a: cfg)
    monkeypatch.setattr(agent, "setup_logging", lambda *a: None)
    monkeypatch.setattr(agent, "UpdateLock", Mock())
    monkeypatch.setattr(agent, "checkin", lambda *a: dict(version="1.1.0"))
    monkeypatch.setattr(agent, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(agent, "run_update", lambda *a: False)
    next_offer = Mock()
    monkeypatch.setattr(agent, "check_for_update", next_offer)
    assert agent.main() == 1
    next_offer.assert_not_called()


def test_preflight_runs_with_old_installed_updater_without_dirtying_payload(tmp_path):
    import os, shutil, subprocess
    package = tmp_path / "package"
    (package / "code" / "scripts").mkdir(parents=True)
    (package / "code" / "updater").mkdir()
    for name in ("__init__.py", "version_policy.py"):
        shutil.copy2(ROOT / "updater" / name, package / "code" / "updater" / name)
    shutil.copy2(ROOT / "scripts" / "ota-preflight.py", package / "code" / "scripts")
    (package / "manifest.json").write_text(json.dumps(dict(version="1.23.10", min_version="1.23.9")))
    (package / "code" / ".version").write_text("1.23.10")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / ".version").write_text("1.23.9")
    command = [sys.executable, str(package / "code" / "scripts" / "ota-preflight.py")]
    result = subprocess.run(command, env={**os.environ, "ZENPLUS_DIR": str(installed)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not list((package / "code").rglob("__pycache__"))
    (installed / ".version").write_text("1.23.8")
    result = subprocess.run(command, env={**os.environ, "ZENPLUS_DIR": str(installed)}, capture_output=True, text=True)
    assert result.returncode != 0
    assert "requires 1.23.9" in result.stderr
