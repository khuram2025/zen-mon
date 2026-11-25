"""The restarted API gets time to start before the OTA health check.

2026-11-23: appliance 10.51.2.12 rolled back 1.25.18 -> 1.25.25 and -> 1.25.26
at the health check (5 attempts, 3 s apart, all "connection refused"), while
1.25.24 passed on its fifth attempt ~15 s after the API started. The release
now waits in a hook from the signed payload (so already-installed updaters
wait too) and the updater's own check retries until a deadline.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
HOOK = "code/scripts/ota-wait-api.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


@pytest.fixture(scope="module")
def hook():
    return _load("zenplus_ota_wait_api", SCRIPTS / "ota-wait-api.py")


@pytest.fixture(scope="module")
def builder():
    return _load("zenplus_build_release_api_wait", SCRIPTS / "build-release.py")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_waits_through_refused_connections_until_the_api_answers(hook):
    clock = FakeClock()
    answers = iter([False] * 8 + [True])          # ~16 s refused, like 10.51.2.12
    ok, waited, state = hook.wait(180, 2, probe=lambda: next(answers),
                                  state=lambda: {"ActiveState": "active"},
                                  clock=clock, sleep=clock.sleep)
    assert ok and waited == 16 and state is None


def test_gives_up_at_the_deadline_with_the_unit_state(hook):
    clock = FakeClock()
    ok, waited, state = hook.wait(180, 2, probe=lambda: False,
                                  state=lambda: {"ActiveState": "activating", "NRestarts": "3"},
                                  clock=clock, sleep=clock.sleep)
    assert not ok and waited == 180 and state["NRestarts"] == "3"


def test_stops_early_when_systemd_gave_up_on_the_unit(hook):
    clock = FakeClock()
    ok, waited, state = hook.wait(180, 2, probe=lambda: False,
                                  state=lambda: {"ActiveState": "failed", "ExecMainStatus": "1"},
                                  clock=clock, sleep=clock.sleep)
    assert not ok and waited == 0
    assert hook.describe(state).startswith("failed/")


def test_failure_names_the_reason_and_redacts_log_secrets(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "wait", lambda: (False, 180, {"ActiveState": "activating", "SubState": "auto-restart",
                                                              "NRestarts": "4", "ExecMainStatus": "1"}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="RuntimeError: bad config\nDATABASE_PASSWORD=hunter2 refused\n"))
    assert hook.main() == 1
    out, err = capsys.readouterr()
    assert "RuntimeError: bad config" in out
    assert "hunter2" not in out and "[redacted]" in out
    assert "did not answer within 180 s" in err and "4 automatic restarts" in err


def test_success_prints_how_long_it_took(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "wait", lambda: (True, 15.2, None))
    assert hook.main() == 0
    assert "answered after 15 s" in capsys.readouterr().out


def test_answered_is_false_when_nothing_listens(hook):
    assert hook.answered("http://127.0.0.1:9/", timeout=1) is False


def test_hook_runs_before_every_health_check_forward_and_rollback(builder):
    assert builder.API_WAIT_STEP["script"] == HOOK
    assert builder.API_WAIT_STEP["timeout"] > hook_wait_s()
    source = (SCRIPTS / "build-release.py").read_text()
    forward_health = source.index('steps.append({"type": "health_check"')
    assert source.rindex("steps.append(API_WAIT_STEP)", 0, forward_health) > \
        source.rindex('script": NCM_UNIT_VERIFIER', 0, forward_health)
    rollback = source.index('"rollback_steps"')
    rollback_health = source.index('{"type": "health_check"', rollback)
    assert source.rindex("API_WAIT_STEP,", rollback, rollback_health) > source.rindex('"start_services"', rollback, rollback_health)
    assert source.count('"type": "health_check"') == 2


def hook_wait_s():
    return _load("zenplus_ota_wait_api_limits", SCRIPTS / "ota-wait-api.py").WAIT_S


def test_hook_ships_in_the_signed_payload_and_is_executable(builder, tmp_path):
    build = tmp_path / "build"
    shutil.copytree(SCRIPTS, build / "code" / "scripts",
                    ignore=shutil.ignore_patterns(*builder.SCRIPT_CODE_IGNORE))
    assert HOOK in builder.release_source.payload_inventory(build)
    # The updater executes hooks directly: shebang + executable bit in git.
    assert (SCRIPTS / "ota-wait-api.py").read_text().startswith("#!/usr/bin/python3\n")
    mode = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "scripts/ota-wait-api.py"],
                          capture_output=True, text=True).stdout.split()
    assert mode and mode[0] == "100755"


def _updater_health():
    sys.path.insert(0, str(ROOT))
    try:
        from updater import health
    finally:
        sys.path.remove(str(ROOT))
    return health


def test_updater_health_check_keeps_trying_until_the_deadline(monkeypatch):
    health = _updater_health()
    clock = FakeClock()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(clock())
        if clock() < 15:
            raise httpx.ConnectError("[Errno 111] Connection refused")
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(health.httpx, "get", fake_get)
    assert health.check_http("http://localhost:8000/api/v1/system/health",
                             clock=clock, sleep=clock.sleep) is True
    assert len(calls) == 6                         # the old limit (5) would have failed


def test_updater_health_check_fails_after_deadline_and_minimum_attempts(monkeypatch):
    health = _updater_health()
    clock = FakeClock()

    def refused(url, **kwargs):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(health.httpx, "get", refused)
    with pytest.raises(health.HealthCheckError, match=r"after 61 attempts in 180 s"):
        health.check_http("http://x/", wait_s=180, clock=clock, sleep=clock.sleep)
    clock2 = FakeClock()
    with pytest.raises(health.HealthCheckError, match=r"after 5 attempts"):
        health.check_http("http://x/", wait_s=0, clock=clock2, sleep=clock2.sleep)
