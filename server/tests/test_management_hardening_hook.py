"""scripts/setup-management-hardening.py runs as root in every install and update (via setup-security.sh).

It validates the SSH configuration with `sshd -t`, which fails with "Missing privilege separation directory:
/run/sshd" on an appliance whose ssh.service has not started since boot (Ubuntu socket activation). That failed
every update on such an appliance. The script is run here against a temporary root: no real system file is
touched and no command is executed.
"""
from __future__ import annotations

import os
import runpy
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-management-hardening.py"
NGINX = "http {\n    access_log /var/log/nginx/access.log;\n}\n"


def run_hook(monkeypatch, root: Path, *, sshd_running: bool, valid_config: bool = True):
    """Run the hook with every absolute path redirected under `root`; return the commands it ran."""
    def mapped(path: Path) -> Path:
        text = str(path)
        return path if text.startswith(str(root)) or not text.startswith("/") else root / text.lstrip("/")

    real = {name: getattr(Path, name) for name in ("mkdir", "write_text", "read_text", "exists", "chmod")}
    commands: list[tuple[list[str], bool]] = []

    def fake_run(command, **kwargs):
        assert kwargs.get("check") is True, "a failed validation must fail the hook"
        commands.append((list(command), real["exists"](root / "run" / "sshd")))
        if command == ["/usr/sbin/sshd", "-t"] and not (valid_config and real["exists"](root / "run" / "sshd")):
            raise subprocess.CalledProcessError(255, command)
        return subprocess.CompletedProcess(command, 0)

    (root / "etc" / "nginx").mkdir(parents=True)
    (root / "etc" / "nginx" / "nginx.conf").write_text(NGINX)
    (root / "run").mkdir()
    if sshd_running:
        (root / "run" / "sshd").mkdir(mode=0o755)
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 0)
        patch.setattr(os, "chown", lambda *a, **k: None)
        patch.setattr(subprocess, "run", fake_run)
        patch.setattr(Path, "mkdir", lambda self, *a, **k: real["mkdir"](mapped(self), *a, **k))
        patch.setattr(Path, "write_text", lambda self, *a, **k: real["write_text"](mapped(self), *a, **k))
        patch.setattr(Path, "read_text", lambda self, *a, **k: real["read_text"](mapped(self), *a, **k))
        patch.setattr(Path, "exists", lambda self, *a, **k: real["exists"](mapped(self), *a, **k))
        patch.setattr(Path, "chmod", lambda self, *a, **k: real["chmod"](mapped(self), *a, **k))
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return commands


@pytest.mark.parametrize("sshd_running", [False, True])
def test_ssh_check_passes_whether_or_not_sshd_has_started_since_boot(monkeypatch, tmp_path, sshd_running):
    commands = run_hook(monkeypatch, tmp_path, sshd_running=sshd_running)
    sshd = [present for command, present in commands if command == ["/usr/sbin/sshd", "-t"]]
    assert sshd == [True], "the privilege separation directory exists when the configuration is checked"
    assert (tmp_path / "run" / "sshd").is_dir()
    assert [command for command, _ in commands] == [
        ["/usr/sbin/sshd", "-t"], ["nginx", "-t"], ["systemctl", "daemon-reload"]]
    # The hardening itself is unchanged.
    policy = (tmp_path / "etc/ssh/sshd_config.d/00-zenplus-security.conf").read_text()
    assert "PermitRootLogin no" in policy and "MaxAuthTries 3" in policy
    assert "access_log /var/log/nginx/access.log zenplus_safe;" in (tmp_path / "etc/nginx/nginx.conf").read_text()


def test_an_invalid_ssh_configuration_still_fails_the_hook(monkeypatch, tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        run_hook(monkeypatch, tmp_path, sshd_running=True, valid_config=False)
