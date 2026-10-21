"""bin/zenplus-apply-ntp.sh must apply what the administrator typed and persist it.

The console CLI (bin/zenplus-cli) and its network helper were retired (ZP-07):
neither was installed nor granted sudo. The NTP helper still ships as a
managed release file, so its input validation stays covered here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NTP_HELPER = ROOT / "bin" / "zenplus-apply-ntp.sh"


def test_retired_cli_and_network_helper_are_gone():
    assert not (ROOT / "bin" / "zenplus-cli").exists()
    assert not (ROOT / "bin" / "zenplus-apply-network.sh").exists()


# ── the NTP helper itself (as root it writes /etc; here into a sandbox) ──────

def run_helper(tmp_path, *args):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for name in ("timedatectl", "systemctl", "chown"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "ZENPLUS_TIMESYNCD_DIR": str(tmp_path / "conf.d")}
    result = subprocess.run(["bash", str(NTP_HELPER), *args], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_helper_writes_a_persistent_dropin_and_restarts_time_sync(tmp_path):
    result, calls = run_helper(tmp_path, "pool.ntp.org", "10.0.0.9")
    assert result.returncode == 0, result.stderr
    conf = tmp_path / "conf.d" / "10-zenplus.conf"
    assert conf.read_text() == "[Time]\nNTP=pool.ntp.org 10.0.0.9\n"
    assert oct(conf.stat().st_mode & 0o777) == "0o644"
    assert "timedatectl set-ntp true" in calls and "systemctl restart systemd-timesyncd" in calls
    assert not [p for p in (tmp_path / "conf.d").iterdir() if p.name.startswith(".")], "temp file left behind"


def test_helper_replaces_the_previous_servers(tmp_path):
    run_helper(tmp_path, "old.example.com")
    run_helper(tmp_path, "new.example.com")
    assert (tmp_path / "conf.d" / "10-zenplus.conf").read_text() == "[Time]\nNTP=new.example.com\n"


@pytest.mark.parametrize("args", [
    [], ["a", "b", "c", "d", "e"], ["bad host"], ["x;reboot"], ["-flag"], ["a\nNTP=evil"], ["$(id)"], ["a.b."],
])
def test_helper_rejects_bad_input_and_changes_nothing(tmp_path, args):
    result, calls = run_helper(tmp_path, *args)
    assert result.returncode == 2
    assert calls == [] and not (tmp_path / "conf.d" / "10-zenplus.conf").exists()
