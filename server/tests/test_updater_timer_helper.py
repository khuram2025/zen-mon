"""Updater check-interval privileges (N2, ZP-05).

The API used to hold `sudo tee .../zenplus-updater.timer.d/override.conf`,
which wrote whatever it was given (Unit=reboot.target, a [Service] section).
It now passes a number to a root helper that writes a fixed template; the OTA
hook installs the helper and the new grant and drops the tee line and the
all-units polkit pkla on upgraded appliances.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "zenplus-updater-timer-helper"
PRIVILEGES = ROOT / "scripts" / "setup-updater-privileges.sh"


@pytest.fixture(scope="module")
def helper():
    loader = importlib.machinery.SourceFileLoader("updater_timer_helper_under_test", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Systemctl:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="boom")


def test_set_interval_writes_only_the_fixed_template(helper, tmp_path):
    dropins = tmp_path / "zenplus-updater.timer.d"
    run = Systemctl()
    assert helper.main(["set-interval", "6"], dropin_dir=dropins, run=run, require_root=False) == 0
    text = (dropins / "override.conf").read_text()
    assert text == ("# Managed by zenplus-updater-timer-helper — do not edit by hand.\n"
                    "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=6h\n")
    assert run.calls == [["/bin/systemctl", "daemon-reload"],
                         ["/bin/systemctl", "restart", "zenplus-updater.timer"]]
    assert (dropins / "override.conf").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("argv", [
    ["set-interval", "0"], ["set-interval", "169"], ["set-interval", "-1"],
    ["set-interval", "4h"], ["set-interval", "4\nUnit=reboot.target"],
    ["set-interval", "４"], ["set-interval", " 4"], ["set-interval"],
    ["set-interval", "4", "extra"], ["reconcile", "x"], ["tee"], [],
])
def test_anything_but_a_bounded_number_is_refused_and_nothing_is_written(helper, tmp_path, argv):
    dropins = tmp_path / "d"
    run = Systemctl()
    assert helper.main(argv, dropin_dir=dropins, run=run, require_root=False) == 1
    assert not dropins.exists()
    assert run.calls == []


def test_symlinked_dropin_paths_are_not_written_through(helper, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "d"
    link.symlink_to(target, target_is_directory=True)
    assert helper.main(["set-interval", "4"], dropin_dir=link, run=Systemctl(), require_root=False) == 1
    assert not any(target.iterdir())

    real = tmp_path / "real"
    real.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (real / "override.conf").symlink_to(victim)
    assert helper.main(["set-interval", "4"], dropin_dir=real, run=Systemctl(), require_root=False) == 0
    assert victim.read_text() == "keep\n"
    assert not (real / "override.conf").is_symlink()


def test_helper_requires_root(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)
    assert helper.main(["set-interval", "4"], dropin_dir=tmp_path / "d", run=Systemctl()) == 1


@pytest.mark.parametrize("existing,expected", [
    ("# Managed by zenplus-api — do not edit by hand.\n[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=12h\n",
     "rewritten (12h)"),
    ("[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=4h\nUnit=reboot.target\n", "removed"),
    ("[Service]\nExecStart=/bin/sh\n", "removed"),
    ("# Managed by zenplus-api — do not edit by hand.\n[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=999h\n",
     "removed"),
])
def test_reconcile_keeps_a_managed_interval_and_drops_anything_else(helper, tmp_path, existing, expected):
    dropins = tmp_path / "d"
    dropins.mkdir()
    (dropins / "override.conf").write_text(existing)
    run = Systemctl()
    assert helper.reconcile(dropins) == expected
    if expected == "removed":
        assert not (dropins / "override.conf").exists()
    else:
        assert (dropins / "override.conf").read_text() == helper.render(12)
    assert helper.reconcile(dropins) == "unchanged"
    assert helper.main(["reconcile"], dropin_dir=dropins, run=run, require_root=False) == 0
    assert run.calls == []  # nothing changed, so no reload


def test_reconcile_without_a_dropin_is_a_noop(helper, tmp_path):
    assert helper.reconcile(tmp_path / "absent") == "unchanged"


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, tmp_path):
    from app.api.v1 import system_updates
    monkeypatch.setattr(system_updates, "TYPED_PRIVILEGE_BROKER", str(tmp_path / "no-broker"))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(system_updates.subprocess, "run", fake_run)
    return system_updates, calls


def test_api_passes_only_the_number_to_the_helper(api):
    system_updates, calls = api
    assert system_updates._write_timer_override(6) == (True, "")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["sudo", "-n", "/usr/local/sbin/zenplus-updater-timer-helper", "set-interval", "6"]
    assert "input" not in kwargs, "no drop-in text crosses the privilege boundary"


def test_api_keeps_the_partial_result_shape_on_failure(api, monkeypatch):
    system_updates, _ = api
    monkeypatch.setattr(system_updates.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="sudo: a password is required"))
    ok, err = system_updates._write_timer_override(4)
    assert not ok and "a password is required" in err
    ok, err = system_updates._write_timer_override(500)
    assert not ok and "1 through 168" in err
    assert system_updates._write_timer_override(0)[0] is False  # clamped to 1, then helper failed


def test_api_uses_the_typed_broker_form_on_the_vmware_image(api, monkeypatch, tmp_path):
    system_updates, calls = api
    broker = tmp_path / "broker"
    broker.write_text("")
    monkeypatch.setattr(system_updates, "TYPED_PRIVILEGE_BROKER", str(broker))
    assert system_updates._write_timer_override(24) == (True, "")
    tee = [c for c in calls if "/usr/bin/tee" in c[0]][0]
    # Exactly what appliance/vmware/privileged.py accepts.
    pattern = r"# Managed by zenplus-api — do not edit by hand[.]\n\[Timer\]\nOnUnitActiveSec=\nOnUnitActiveSec=([0-9]{1,3})h\n"
    assert re.fullmatch(pattern, tee[1]["input"]).group(1) == "24"
    broker_source = (ROOT / "appliance" / "vmware" / "privileged.py").read_text()
    assert "# Managed by zenplus-api — do not edit by hand[.]" in broker_source
    assert "OnUnitActiveSec=([0-9]{1,3})h" in broker_source


# ── Installed policy ─────────────────────────────────────────────────────────

def test_no_free_form_write_grant_remains():
    sources = [PRIVILEGES, ROOT / "install.sh", ROOT / "scripts" / "setup-updater.sh"]
    for path in sources:
        text = path.read_text()
        assert "NOPASSWD: /usr/bin/tee" not in text, path
        assert "zenplus-updater.pkla" not in text or path == PRIVILEGES, path
    policy = PRIVILEGES.read_text()
    grants = re.findall(r"^\$\{SERVICE_USER\} ALL=\(root\) NOPASSWD: (.+)$", policy, re.M)
    assert grants == [
        "/bin/systemctl --no-block start zenplus-updater.service",
        "/bin/systemctl start zenplus-updater.service",
        "${HELPER_DST} set-interval *",
    ]
    assert not (ROOT / "updater" / "polkit" / "zenplus-updater.pkla").exists()
    assert (ROOT / "updater" / "polkit" / "50-zenplus-updater.rules").exists()


def test_every_api_sudo_call_is_granted():
    """Each privileged call in system_updates.py has a matching sudo line, so
    nothing depends on polkit (which 20.04/22.04 no longer get)."""
    api = (ROOT / "server" / "app" / "api" / "v1" / "system_updates.py").read_text()
    assert '["sudo", "-n", "/bin/systemctl", "--no-block", "start",\n             "zenplus-updater.service"]' in api
    assert '["sudo", "-n", TIMER_HELPER, "set-interval", str(hours)]' in api
    calls = re.findall(r'\["systemctl", "(\w[\w-]*)"', api)
    assert set(calls) <= {"show", "is-active"}, "unprivileged systemctl calls must stay read-only"


def test_privileges_hook_runs_on_every_ota_and_from_the_installers():
    build = (ROOT / "scripts" / "build-release.py").read_text()
    assert '"code/scripts/setup-updater-privileges.sh"' in build
    start = build.index('"script": "code/scripts/setup-updater-privileges.sh"')
    assert start < build.index('steps.append({"type": "start_services"')
    assert "setup-updater-privileges.sh" in (ROOT / "install.sh").read_text()
    assert "setup-updater-privileges.sh" in (ROOT / "scripts" / "setup-updater.sh").read_text()


def test_privileges_hook_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(PRIVILEGES)]).returncode == 0
    assert PRIVILEGES.stat().st_mode & 0o111 and HELPER.stat().st_mode & 0o111
