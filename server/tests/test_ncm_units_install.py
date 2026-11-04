"""NCCM worker/timer units are installed, enabled and verified on every path.

A fresh 1.24.48 install left zenplus-ncm-worker.service and the backup timer
uninstalled (the NCCM page showed both inactive). install.sh now installs the
units, the signed OTA manifest installs the same list from its payload, and
both verify the result with scripts/verify-ncm-units.py. The readiness API
tells "not installed" apart from "inactive".
"""
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UNIT_DIR = SCRIPTS / "systemd"
ENABLED = {"zenplus-ncm-worker.service", "zenplus-ncm-backup.timer", "zenplus-ncm-delivery.timer"}
ALL = ENABLED | {"zenplus-ncm-backup.service", "zenplus-ncm-delivery.service"}


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
def builder():
    return _load("zenplus_build_release_ncm", SCRIPTS / "build-release.py")


@pytest.fixture(scope="module")
def verifier():
    return _load("zenplus_verify_ncm_units", SCRIPTS / "verify-ncm-units.py")


def test_unit_lists_agree_everywhere(builder, verifier):
    assert {u for u, _ in builder.NCM_UNITS} == ALL
    assert set(builder.ncm_enabled_units()) == ENABLED
    assert set(verifier.NCM_UNITS) == ALL
    assert {u for u, (_, enabled, _) in verifier.NCM_UNITS.items() if enabled} == ENABLED
    for unit in ALL:
        assert (UNIT_DIR / unit).is_file(), unit
    install = (ROOT / "install.sh").read_text()
    assert "NCM_UNIT_FILES=(zenplus-ncm-worker.service zenplus-ncm-backup.service zenplus-ncm-backup.timer\n" \
           "                zenplus-ncm-delivery.service zenplus-ncm-delivery.timer)" in install
    assert "NCM_ENABLED_UNITS=(zenplus-ncm-worker.service zenplus-ncm-backup.timer zenplus-ncm-delivery.timer)" in install
    # Timers and the worker install for boot; oneshot services are timer-started.
    for unit in ENABLED:
        assert "[Install]" in (UNIT_DIR / unit).read_text()


def _stage_payload(builder, tmp_path):
    """The build's own copy rules for code/scripts, then its signed inventory."""
    build = tmp_path / "build"
    shutil.copytree(SCRIPTS, build / "code" / "scripts",
                    ignore=shutil.ignore_patterns(*builder.SCRIPT_CODE_IGNORE))
    return build


def test_ota_manifest_steps_install_and_enable_units_from_the_signed_payload(builder, tmp_path):
    build = _stage_payload(builder, tmp_path)
    steps = builder.ncm_unit_steps(build)
    assert [(s["type"], Path(s["source"]).name, s["enable"]) for s in steps] == \
           [("install_systemd", u, e) for u, e in builder.NCM_UNITS]
    inventory = builder.release_source.payload_inventory(build)
    for step in steps:
        assert step["source"] in inventory, step["source"]                   # signed and checksummed
    assert builder.NCM_UNIT_VERIFIER in inventory
    # Updater executes hooks directly: shebang + executable bit in git.
    assert (SCRIPTS / "verify-ncm-units.py").read_text().startswith("#!/usr/bin/python3\n")
    mode = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "scripts/verify-ncm-units.py"],
                          capture_output=True, text=True).stdout.split()
    assert mode and mode[0] == "100755"


def test_ota_build_refuses_payload_without_a_unit(builder, tmp_path):
    build = _stage_payload(builder, tmp_path)
    (build / "code" / "scripts" / "systemd" / "zenplus-ncm-backup.timer").unlink()
    with pytest.raises(RuntimeError, match="zenplus-ncm-backup.timer"):
        builder.ncm_unit_steps(build)


def test_ota_manifest_starts_then_verifies_before_health_check():
    source = (SCRIPTS / "build-release.py").read_text()
    install = source.index("steps.extend(ncm_unit_steps(build_dir))")
    start = source.index('"enabled_services": ["zenplus-syslog", *ncm_enabled_units()]')
    verify = source.index('steps.append({"type": "run_hook", "script": NCM_UNIT_VERIFIER')
    health = source.index('steps.append({"type": "health_check"', verify - 400)
    assert install < start < verify < health
    # Stopped before the code swap so the worker never runs mixed code.
    stop = source.index('steps.append({"type": "stop_services"')
    assert '"zenplus-ncm-worker.service"' in source[stop:stop + 400]


def test_install_function_installs_enables_and_starts(tmp_path):
    """Run the real install_ncm_units with install/systemctl stubbed."""
    definitions = tmp_path / "defs.sh"
    definitions.write_text((ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0])
    home = tmp_path / "zenplus"
    shutil.copytree(UNIT_DIR, home / "scripts" / "systemd")
    log = tmp_path / "calls.log"
    script = f"""
source {shlex.quote(str(definitions))}
ZENPLUS_HOME={shlex.quote(str(home))}
install() {{ echo "install $*" >> {shlex.quote(str(log))}; }}
systemctl() {{ echo "systemctl $*" >> {shlex.quote(str(log))}; }}
install_ncm_units; echo "rc=$?"
"""
    env = dict(os.environ, INSTALL_LOG=str(tmp_path / "install.log"))
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    installed = {Path(c.split()[-1]).name for c in calls if c.startswith("install -m 0644 ")}
    assert installed == ALL
    assert all(c.split()[-1].startswith("/etc/systemd/system/") for c in calls if c.startswith("install "))
    reload_at = calls.index("systemctl daemon-reload")
    enable = next(i for i, c in enumerate(calls) if c.startswith("systemctl enable "))
    restart = next(i for i, c in enumerate(calls) if c.startswith("systemctl restart "))
    assert max(i for i, c in enumerate(calls) if c.startswith("install ")) < reload_at < enable < restart
    assert set(calls[enable].split()[2:]) == ENABLED and set(calls[restart].split()[2:]) == ENABLED

    (home / "scripts" / "systemd" / "zenplus-ncm-worker.service").unlink()
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "rc=1" in result.stdout and "zenplus-ncm-worker.service missing" in result.stdout


def test_installer_wires_install_and_verification():
    install = (ROOT / "install.sh").read_text()
    create = install[install.index("create_services() {"):install.index('log "All services created and started"')]
    assert "install_ncm_units ||" in create
    verify = install[install.index("verify_installation() {"):]
    verify = verify[:verify.index("\n}\n")]
    assert 'scripts/verify-ncm-units.py" --wait 30' in verify and "failures=$((failures + 1))" in verify
    # The VMware provisioning reuses create_services from install.sh.
    assert "create_services" in (ROOT / "appliance" / "vmware" / "provision.sh").read_text()


def _states():
    """Healthy appliance: worker and timers enabled and active, oneshot services static."""
    return {u: {"load": "loaded", "active": "active" if u in ENABLED else "inactive", "sub": "running",
                "unit_file": "enabled" if u in ENABLED else "static"} for u in ALL}


def test_verifier_reports_each_problem_distinctly(verifier):
    states = _states()
    states["zenplus-ncm-worker.service"].update(load="not-found", active="inactive", unit_file="")
    states["zenplus-ncm-backup.timer"].update(unit_file="disabled")
    states["zenplus-ncm-delivery.timer"].update(active="failed", sub="failed")
    _, problems = verifier.check(0, poll=lambda u: states[u])
    text = "\n".join(problems)
    assert "zenplus-ncm-worker.service (backup worker): NOT INSTALLED" in text
    assert "zenplus-ncm-backup.timer (schedule timer): installed but NOT ENABLED" in text
    assert "zenplus-ncm-delivery.timer (delivery timer): installed but FAILED" in text
    assert len(problems) == 3


def test_verifier_waits_for_a_unit_that_is_still_starting(verifier):
    states = _states()
    states["zenplus-ncm-worker.service"].update(active="activating", sub="auto-restart")
    clock = {"t": 0.0}

    def poll(unit):
        if clock["t"] >= 4:
            states["zenplus-ncm-worker.service"].update(active="active", sub="running")
        return states[unit]
    _, problems = verifier.check(30, poll=poll, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                                 clock=lambda: clock["t"])
    assert problems == [] and clock["t"] >= 4


def test_verifier_does_not_wait_for_a_missing_unit(verifier):
    states = _states()
    states["zenplus-ncm-backup.timer"].update(load="not-found")
    slept = []
    _, problems = verifier.check(30, poll=lambda u: states[u], sleep=slept.append, clock=lambda: 0)
    assert problems and not slept


def test_verifier_exit_code_and_messages(verifier, monkeypatch, capsys):
    states = _states()
    monkeypatch.setattr(verifier, "unit_state", lambda u: states[u])
    assert verifier.main(["--wait", "0"]) == 0
    assert "installed, enabled and active" in capsys.readouterr().out
    states["zenplus-ncm-worker.service"].update(load="not-found")
    monkeypatch.setattr(verifier, "check", lambda wait: (states, verifier.problems_for(
        "zenplus-ncm-worker.service", states["zenplus-ncm-worker.service"])))
    assert verifier.main(["--wait", "0"]) == 1
    err = capsys.readouterr().err
    assert "FAIL zenplus-ncm-worker.service (backup worker): NOT INSTALLED" in err
    assert "systemctl enable --now zenplus-ncm-worker.service" in err


# --------------------------------------------------------------------------- #
# Readiness API: not installed vs inactive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("load,active,expected", [
    ("not-found", "inactive", "not_installed"), ("loaded", "inactive", "inactive"),
    ("loaded", "failed", "inactive"), ("loaded", "activating", "inactive"), ("loaded", "active", "active"),
    ("masked", "inactive", "unknown"), (None, None, "unknown"),
])
def test_unit_runtime_state(load, active, expected):
    from app.services.ncm_readiness import unit_runtime_state
    assert unit_runtime_state(load, active) == expected


def test_runtime_health_reads_systemctl_show(monkeypatch):
    from app.services import ncm_readiness
    outputs = {
        "zenplus-ncm-worker.service": "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n",
        "zenplus-ncm-backup.timer": "LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n",
        "zenplus-ncm-delivery.timer": "LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n",
    }

    class Done:
        def __init__(self, stdout):
            self.stdout = stdout
    monkeypatch.setattr(ncm_readiness.subprocess, "run",
                        lambda argv, **kw: Done(outputs.get(argv[2], "") if argv[:2] == ["systemctl", "show"] else ""))
    ncm_readiness._runtime_bucket.cache_clear()
    result = ncm_readiness._runtime_bucket(-1)
    ncm_readiness._runtime_bucket.cache_clear()
    assert (result["worker"], result["scheduler"], result["delivery"]) == ("not_installed", "inactive", "active")
    assert result["units"]["scheduler"] == {"unit": "zenplus-ncm-backup.timer", "state": "inactive", "load": "loaded",
                                            "active": "inactive", "sub": "dead", "enabled": "disabled"}


def _device(**changes):
    return dict(device_id="fixture", eligible=True, eligibility_reason="fixture", vendor="Fortinet",
                platform="fortinet", config_types=["running"], profile_configured=True, ncm_enabled=True,
                fresh=True, schedule_enabled=True, **changes)


def test_readiness_names_a_missing_worker_and_timer():
    from app.services.ncm_readiness import device_readiness
    result = device_readiness(_device(), dict(worker="not_installed", scheduler="not_installed",
                                              delivery="not_installed", encryption="configured"))
    checks = {c["id"]: c for c in result["checks"]}
    assert not result["can_capture"] and checks["worker"]["status"] == "blocked"
    assert "not installed" in checks["worker"]["detail"] and "installer" in checks["worker"]["detail"]
    assert "not installed" in checks["schedule"]["detail"] and checks["schedule"]["status"] == "pending"
    assert "not installed" in checks["notifications"]["detail"]
    inactive = {c["id"]: c for c in device_readiness(_device(), dict(
        worker="inactive", scheduler="inactive", delivery="active", encryption="configured"))["checks"]}
    assert "installed but not running" in inactive["worker"]["detail"]
    assert "not running" in inactive["schedule"]["detail"]
