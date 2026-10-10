"""Storage changes must survive an OTA rollback performed by any updater.

setup-storage.sh changes the host (watchdog unit, ClickHouse bind mounts) and
an OTA rollback restores and prunes /opt/zenplus. These tests run the shell
scripts with stub ``docker``/``systemctl`` binaries in temporary trees.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell scripts")

RECONCILE = ROOT / "scripts" / "ota-reconcile-storage-rollback.sh"
INSTALL_CONF = ROOT / "scripts" / "install-clickhouse-config.sh"


def stubs(tmp_path: Path, mounts: list[str], health="healthy", compose_rc=0) -> tuple[dict, Path]:
    """Stub docker/systemctl that log calls; docker inspect reports ``mounts``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    docker = f"""#!/bin/bash
echo "docker $*" >> "{log}"
if [ "$1" = "inspect" ]; then
  case "$*" in
    *".Mounts"*) printf '%s\\n' {" ".join(repr(m) for m in mounts) or "''"} ;;
    *".State.Health.Status"*) echo "{health}" ;;
  esac
  exit 0
fi
if [ "$1" = "compose" ]; then exit {compose_rc}; fi
exit 0
"""
    systemctl = f'#!/bin/bash\necho "systemctl $*" >> "{log}"\nexit 0\n'
    for name, body in (("docker", docker), ("systemctl", systemctl), ("sleep", "#!/bin/sh\nexit 0\n")):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return env, log


def restored_tree(tmp_path: Path, with_watchdog: bool) -> Path:
    zen = tmp_path / "opt"
    (zen / "server" / "app" / "services").mkdir(parents=True)
    if with_watchdog:
        (zen / "server" / "app" / "services" / "storage_watchdog.py").write_text("# restored\n")
    return zen


def run_reconcile(tmp_path, env, zen, unit, state):
    env = dict(env, ZENPLUS_DIR=str(zen), ZENPLUS_WATCHDOG_UNIT=str(unit),
               ZENPLUS_STORAGE_STATE_DIR=str(state))
    return subprocess.run(["bash", str(RECONCILE)], env=env, capture_output=True, text=True, timeout=30)


def test_rollback_to_a_version_without_the_watchdog_removes_its_unit_and_pause_marker(tmp_path):
    present = tmp_path / "etc-conf.xml"
    present.write_text("<clickhouse/>")
    env, log = stubs(tmp_path, [str(present)])
    zen = restored_tree(tmp_path, with_watchdog=False)
    unit = tmp_path / "zenplus-storage-watchdog.service"
    unit.write_text("[Unit]\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "ingest-paused").write_text("{}")
    r = run_reconcile(tmp_path, env, zen, unit, state)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert "systemctl disable --now zenplus-storage-watchdog.service" in calls
    assert "systemctl daemon-reload" in calls
    assert not unit.exists() and not (state / "ingest-paused").exists()
    assert "compose" not in calls                       # mount sources intact: no recreate


def test_rollback_keeps_and_restarts_the_watchdog_when_the_restored_code_has_it(tmp_path):
    env, log = stubs(tmp_path, [])
    zen = restored_tree(tmp_path, with_watchdog=True)
    unit = tmp_path / "unit.service"
    unit.write_text("[Unit]\n")
    r = run_reconcile(tmp_path, env, zen, unit, tmp_path)
    assert r.returncode == 0
    assert "systemctl try-restart zenplus-storage-watchdog.service" in log.read_text()
    assert unit.exists()


def test_missing_bind_source_recreates_clickhouse_from_the_restored_compose(tmp_path):
    # The 1.24.1 layout: config mounted from the code tree, then pruned by rollback.
    gone = tmp_path / "opt" / "docker" / "clickhouse" / "zenplus_logging.xml"
    env, log = stubs(tmp_path, [str(gone)])
    zen = restored_tree(tmp_path, with_watchdog=False)
    r = run_reconcile(tmp_path, env, zen, tmp_path / "none.service", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker compose up -d --force-recreate clickhouse" in log.read_text()
    assert "recreated (healthy)" in r.stdout


def test_docker_created_directory_in_place_of_a_config_file_is_repaired(tmp_path):
    bogus = tmp_path / "opt" / "docker" / "clickhouse" / "zenplus_profile.xml"
    bogus.mkdir(parents=True)                          # what Docker leaves behind
    env, log = stubs(tmp_path, [str(bogus)])
    zen = tmp_path / "opt"
    (zen / "server" / "app" / "services").mkdir(parents=True)
    r = run_reconcile(tmp_path, env, zen, tmp_path / "none.service", tmp_path)
    assert r.returncode == 0
    assert not bogus.exists()
    assert "--force-recreate clickhouse" in log.read_text()


def test_unhealthy_recreate_is_reported_without_aborting(tmp_path):
    env, log = stubs(tmp_path, [str(tmp_path / "missing.xml")], health="starting")
    zen = restored_tree(tmp_path, with_watchdog=True)
    r = run_reconcile(tmp_path, env, zen, tmp_path / "none.service", tmp_path)
    assert r.returncode == 1
    assert "not healthy" in r.stdout


def test_no_clickhouse_container_is_a_no_op(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    zen = restored_tree(tmp_path, with_watchdog=True)
    assert run_reconcile(tmp_path, env, zen, tmp_path / "none.service", tmp_path).returncode == 0


# ─── ClickHouse configuration outside the code tree ──────────────────────────

def run_install(tmp_path, zen):
    env = dict(os.environ, ZENPLUS_DIR=str(zen), ZENPLUS_CLICKHOUSE_CONF_DIR=str(tmp_path / "etc"))
    return subprocess.run(["bash", str(INSTALL_CONF)], env=env, capture_output=True, text=True, timeout=30)


def source_tree(tmp_path) -> Path:
    zen = tmp_path / "opt"
    src = zen / "docker" / "clickhouse"
    src.mkdir(parents=True)
    for name in ("zenplus_logging.xml", "zenplus_profile.xml"):
        (src / name).write_text((ROOT / "docker" / "clickhouse" / name).read_text())
    return zen


def test_config_install_is_idempotent_and_replaces_atomically(tmp_path):
    zen = source_tree(tmp_path)
    first = run_install(tmp_path, zen)
    assert first.returncode == 0 and "changed=1" in first.stdout
    dst = tmp_path / "etc" / "config.d" / "zenplus_logging.xml"
    assert dst.read_text() == (ROOT / "docker" / "clickhouse" / "zenplus_logging.xml").read_text()
    assert oct(dst.stat().st_mode & 0o777) == "0o644"
    inode = dst.stat().st_ino
    assert "changed=0" in run_install(tmp_path, zen).stdout
    assert dst.stat().st_ino == inode, "unchanged content is not rewritten"
    (zen / "docker" / "clickhouse" / "zenplus_logging.xml").write_text("<clickhouse><!-- v2 --></clickhouse>")
    third = run_install(tmp_path, zen)
    assert "changed=1" in third.stdout and "v2" in dst.read_text()
    # Renamed into place: a new inode (a running container keeps its old, valid one).
    assert dst.stat().st_ino != inode
    assert not list((tmp_path / "etc" / "config.d").glob(".zenplus-stage.*"))


def test_interrupted_install_never_leaves_partial_config(tmp_path):
    zen = source_tree(tmp_path)
    assert run_install(tmp_path, zen).returncode == 0
    dst = tmp_path / "etc" / "config.d" / "zenplus_logging.xml"
    good = dst.read_text()
    (zen / "docker" / "clickhouse" / "zenplus_logging.xml").write_text("<clickhouse><!-- v2 --></clickhouse>")
    # Simulate the process dying right before the rename (power loss / kill).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mv").write_text("#!/bin/bash\nkill -9 $PPID\n")
    (bin_dir / "mv").chmod(0o755)
    env = dict(os.environ, ZENPLUS_DIR=str(zen), ZENPLUS_CLICKHOUSE_CONF_DIR=str(tmp_path / "etc"),
               PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    killed = subprocess.run(["bash", str(INSTALL_CONF)], env=env, capture_output=True, text=True, timeout=30)
    assert killed.returncode != 0
    assert dst.read_text() == good, "the live file is the old complete document"
    assert list((tmp_path / "etc" / "config.d").glob(".zenplus-stage.*")), "staged copy left behind"
    # The next run cleans up and completes.
    again = run_install(tmp_path, zen)
    assert again.returncode == 0 and "v2" in dst.read_text()
    assert not list((tmp_path / "etc" / "config.d").glob(".zenplus-stage.*"))


def test_invalid_source_xml_is_refused_and_live_config_kept(tmp_path):
    zen = source_tree(tmp_path)
    assert run_install(tmp_path, zen).returncode == 0
    dst = tmp_path / "etc" / "config.d" / "zenplus_logging.xml"
    good = dst.read_text()
    (zen / "docker" / "clickhouse" / "zenplus_logging.xml").write_text("<clickhouse><logger>")
    r = run_install(tmp_path, zen)
    assert r.returncode != 0 and "invalid" in r.stderr
    assert dst.read_text() == good


def test_config_install_repairs_a_docker_created_directory_and_fails_on_missing_source(tmp_path):
    zen = source_tree(tmp_path)
    bogus = tmp_path / "etc" / "users.d" / "zenplus_profile.xml"
    bogus.mkdir(parents=True)
    r = run_install(tmp_path, zen)
    assert r.returncode == 0 and bogus.is_file()
    (zen / "docker" / "clickhouse" / "zenplus_profile.xml").unlink()
    (tmp_path / "etc" / "users.d" / "zenplus_profile.xml").unlink()
    assert run_install(tmp_path, zen).returncode != 0


# ─── Release wiring ─────────────────────────────────────────────────────────

def test_compose_mounts_logging_limits_from_outside_the_code_tree():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "/etc/zenplus/clickhouse/config.d/zenplus_logging.xml:/etc/clickhouse-server/config.d/zenplus_logging.xml:ro" in compose
    assert "/etc/zenplus/clickhouse/users.d/zenplus_profile.xml:/etc/clickhouse-server/users.d/zenplus_profile.xml:ro" in compose
    assert "./docker/clickhouse/zenplus_" not in compose


def test_setup_storage_installs_config_before_recreating_and_checks_mount_sources():
    setup = (ROOT / "scripts" / "setup-storage.sh").read_text()
    install_at = setup.index("install-clickhouse-config.sh")
    assert install_at < setup.index("docker compose up -d --force-recreate clickhouse")
    assert "{{.Source}}:{{.Destination}}" in setup
    # Changed content needs a new container (single-file mounts pin inodes).
    assert '"$CONF_CHANGED" = 0 &&' in setup and "docker restart" not in setup
    # The watchdog is installed before the ClickHouse step, which may stop the script.
    assert setup.index("zenplus-storage-watchdog.service") < setup.index("# ── 6. ClickHouse")


def test_fresh_install_installs_clickhouse_config_before_first_start():
    install = (ROOT / "install.sh").read_text()
    assert install.index("install-clickhouse-config.sh") < install.index("docker compose up -d clickhouse")


def test_release_manifest_runs_the_storage_rollback_hook_from_the_payload():
    src = (ROOT / "scripts" / "build-release.py").read_text()
    block = src[src.index('"rollback_steps": ['):src.index("manifest[INVENTORY_KEY]")]
    restore = block.index("ota-restore-backup.py")
    hook = block.index("ota-reconcile-storage-rollback.sh")
    start = block.index('"type": "start_services"')
    assert restore < hook < start, "after the code is restored, before services start"
    assert re.search(r'"script": "code/scripts/setup-storage.sh",\s*#[^\n]*\n[^\n]*\n\s*"timeout": 600', src)


def test_watchdog_unit_does_not_restart_a_missing_module():
    unit = (ROOT / "updater" / "systemd" / "zenplus-storage-watchdog.service").read_text()
    assert "ConditionPathExists=/opt/zenplus/server/app/services/storage_watchdog.py" in unit


def test_rollback_hook_is_compatible_with_the_1_23_9_executor():
    # 1.23.9 runs hooks as [script_path] (shebang) with cwd /opt/zenplus and
    # ZENPLUS_DIR set; the script must not depend on anything newer.
    text = RECONCILE.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert os.access(RECONCILE, os.X_OK)
    assert "ZENPLUS_DIR" in text and "set -e" not in text.replace("set -euo", "")
