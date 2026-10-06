"""scripts/setup-updater-trust.sh, run in a sandbox with stand-ins for the
commands that need root, must leave the updater's trust out of the service
account's reach without destroying administrator settings."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from updater.trust import load_trust

SCRIPT = ROOT / "scripts" / "setup-updater-trust.sh"
UID = os.getuid()


def make_stubs(directory: Path, *, in_docker_group: bool = True, docker_group_exists: bool = True) -> Path:
    """Fake id/getent/chown/gpasswd that record what the script asked for."""
    directory.mkdir()
    log = directory / "calls.log"
    log.write_text("")

    def stub(name: str, body: str) -> None:
        path = directory / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    stub("id", f'''
case "$1" in
  -u) echo 0 ;;
  -nG) {'echo "zenplus adm docker"' if in_docker_group else 'echo "zenplus adm"'} ;;
  *) /usr/bin/id "$@" ;;
esac
''')
    stub("getent", f'[ "$1" = group ] && [ "$2" = docker ] && exit {0 if docker_group_exists else 2}; exit 0\n')
    stub("chown", f'echo "chown $*" >> "{log}"\n')
    stub("gpasswd", f'echo "gpasswd $*" >> "{log}"\n')
    return log


@pytest.fixture
def box(tmp_path):
    """A fake appliance: /opt/zenplus/updater with code, keys, logs and config."""
    zp = tmp_path / "opt" / "zenplus"
    updater = zp / "updater"
    for sub in ("steps", "keys", "logs", "backups", "config"):
        (updater / sub).mkdir(parents=True)
    (updater / "agent.py").write_text("# code\n")
    (updater / "keys" / "zentryc-release.pub").write_text("key\n")
    (updater / "config" / "agent.conf").write_text("[server]\nurl = https://zentryc.com\n")
    (zp / "scripts").mkdir()
    (zp / "scripts" / "ch_migrate.py").write_text("# loaded by the root updater\n")
    (zp / "bin").mkdir()
    (zp / "venv-real" / "bin").mkdir(parents=True)
    (zp / "venv").symlink_to(zp / "venv-real")
    etc = tmp_path / "etc" / "zenplus"
    for directory in [tmp_path, *[d for d in tmp_path.rglob("*") if d.is_dir()]]:
        directory.chmod(0o755)  # not group-writable whatever this machine's umask is
    return {"zp": zp, "updater": updater, "etc": etc, "tmp": tmp_path}


def run(box, *, stubs: Path | None = None, **stub_opts):
    stubs = stubs or box["tmp"] / "stubs"
    log = make_stubs(stubs, **stub_opts) if not stubs.exists() else stubs / "calls.log"
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "ZENPLUS_DIR": str(box["zp"]),
        "ZENPLUS_ETC_DIR": str(box["etc"]),
        "HOME": str(box["tmp"]),
    }
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return result, log.read_text().splitlines()


def test_a_fresh_appliance_gets_a_default_trust_file_and_key_directory(box):
    result, calls = run(box)
    assert result.returncode == 0, result.stderr
    trust = box["etc"] / "updater-trust.conf"
    assert trust.is_file() and stat.S_IMODE(trust.stat().st_mode) == 0o644
    assert (box["etc"] / "updater-keys.d").is_dir()
    assert any(c.startswith("chown root:root") and "updater-trust.conf" in c for c in calls)


def test_legacy_version_marker_is_protected_without_advancing_it(box):
    marker = box['zp'] / '.version'
    marker.write_text('1.23.31\nold-timestamp\n')
    marker.chmod(0o666)
    result, calls = run(box)
    assert result.returncode == 0, result.stderr
    assert f'chown root:root {marker}' in calls
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert marker.read_text() == '1.23.31\nold-timestamp\n'


@pytest.mark.parametrize('kind', ['symlink', 'dangling_symlink', 'directory'])
def test_invalid_version_marker_is_rejected_without_following_it(box, kind):
    marker = box['zp'] / '.version'
    target = box['tmp'] / 'outside-version'
    if kind == 'directory':
        marker.mkdir()
    else:
        if kind == 'symlink':
            target.write_text('outside\n')
            target.chmod(0o666)
        marker.symlink_to(target)
    result, calls = run(box)
    assert result.returncode != 0 and '.version is not a regular file' in result.stderr
    assert f'chown root:root {marker}' not in calls
    if target.exists():
        assert target.read_text() == 'outside\n'
        assert stat.S_IMODE(target.stat().st_mode) == 0o666


def test_the_generated_trust_file_is_accepted_by_the_updater(box):
    run(box)
    for directory in [box["etc"].parent, box["etc"], box["etc"] / "updater-keys.d"]:
        directory.chmod(0o755)
    trust = load_trust(box["etc"] / "updater-trust.conf",
                       key_dropin_dir=box["etc"] / "updater-keys.d",
                       default_key_path=box["updater"] / "keys" / "zentryc-release.pub",
                       trusted_uid=UID, stop_at=box["tmp"])
    assert trust.server_url == "https://zentryc.com"
    assert trust.verify_tls is True and trust.allow_legacy_packages is False


def test_an_administrators_trust_file_is_never_overwritten(box):
    box["etc"].mkdir(parents=True)
    trust = box["etc"] / "updater-trust.conf"
    trust.write_text("[trust]\nserver_url = https://ota.example.com\n")
    trust.chmod(0o666)
    result, _ = run(box)
    assert result.returncode == 0, result.stderr
    assert "ota.example.com" in trust.read_text()
    assert stat.S_IMODE(trust.stat().st_mode) == 0o644  # but group/other write is removed


def test_running_twice_changes_nothing(box):
    run(box)
    first = (box["etc"] / "updater-trust.conf").read_text()
    stubs = box["tmp"] / "stubs"
    result, _ = run(box, stubs=stubs)
    assert result.returncode == 0
    assert (box["etc"] / "updater-trust.conf").read_text() == first


def test_updater_tree_is_root_owned_with_only_data_files_delegated(box):
    _, calls = run(box)
    recursive = [c for c in calls if c.startswith("chown -h -R root:root")]
    handed = {Path(c.split()[-1]).name for c in recursive}
    assert {"agent.py", "steps", "keys", "logs", "backups"} <= handed
    assert "config" not in handed
    assert not any(c.endswith(f"zenplus:zenplus {box['updater']}/config") for c in calls)
    assert any(c.endswith(f"root:root {box['updater']}/config") for c in calls)
    assert any(c.endswith(f"zenplus:zenplus {box['updater']}/config/agent.conf") for c in calls)
    assert any(c.endswith(f"zenplus:zenplus {box['updater']}/config/subscription.json") for c in calls)


def test_group_and_other_write_is_removed_from_the_updater_tree(box):
    (box["updater"] / "agent.py").chmod(0o666)
    (box["updater"] / "keys").chmod(0o777)
    run(box)
    assert not (box["updater"] / "agent.py").stat().st_mode & 0o022
    assert not (box["updater"] / "keys").stat().st_mode & 0o022


def test_a_planted_symlink_is_removed_and_its_target_left_alone(box):
    victim = box["tmp"] / "victim"
    victim.mkdir()
    victim.chmod(0o777)
    (box["updater"] / "steps" / "evil").symlink_to(victim)
    (box["updater"] / "top-link").symlink_to(victim)
    result, _ = run(box)
    assert result.returncode == 0
    assert not (box["updater"] / "steps" / "evil").exists(follow_symlinks=False)
    assert not (box["updater"] / "top-link").exists(follow_symlinks=False)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o777, "chmod must not follow the link"
    assert "removing unexpected symlinks" in result.stderr


def test_the_service_account_is_removed_from_the_docker_group(box):
    _, calls = run(box)
    assert "gpasswd -d zenplus docker" in calls


def test_nothing_happens_when_the_account_is_not_in_the_docker_group(box):
    _, calls = run(box, in_docker_group=False)
    assert not any(c.startswith("gpasswd") for c in calls)


def test_a_host_without_docker_is_fine(box):
    result, calls = run(box, docker_group_exists=False)
    assert result.returncode == 0
    assert not any(c.startswith("gpasswd") for c in calls)


def test_a_custom_server_url_in_agent_conf_is_reported_not_imported(box):
    (box["updater"] / "config" / "agent.conf").write_text(
        "[server]\nurl = https://evil.example.com\n[security]\nverify_tls = false\n")
    result, _ = run(box)
    assert "evil.example.com" in result.stderr and "ignored" in result.stderr
    assert "TLS verification" in result.stderr
    assert "evil.example.com" not in (box["etc"] / "updater-trust.conf").read_text()


def test_it_refuses_to_run_as_a_normal_user(box):
    stubs = box["tmp"] / "stubs"
    make_stubs(stubs)
    (stubs / "id").write_text('#!/bin/sh\n[ "$1" = -u ] && echo 1000\n')
    result, _ = run(box, stubs=stubs)
    assert result.returncode == 1 and "must run as root" in result.stderr


def test_everything_the_root_updater_executes_is_handed_to_root(box):
    _, calls = run(box)
    zp = box["zp"]
    assert f"chown root:root {zp}" in calls, "the directory the updater imports from"
    recursive = {c.split()[-1] for c in calls if c.startswith("chown -h -R root:root")}
    assert {str(zp / "scripts"), str(zp / "bin")} <= recursive
    # The venv symlink is resolved, never chowned through the link.
    assert str(zp / "venv-real") in recursive and str(zp / "venv") not in recursive


def test_group_and_other_write_is_removed_from_scripts_and_the_venv(box):
    (box["zp"] / "scripts" / "ch_migrate.py").chmod(0o666)
    (box["zp"] / "scripts").chmod(0o777)
    (box["zp"] / "venv-real").chmod(0o777)
    run(box)
    assert not (box["zp"] / "scripts" / "ch_migrate.py").stat().st_mode & 0o022
    assert not (box["zp"] / "scripts").stat().st_mode & 0o022
    assert not (box["zp"] / "venv-real").stat().st_mode & 0o022
    assert not box["zp"].stat().st_mode & 0o022


def test_a_venv_that_points_outside_the_install_is_left_alone(box):
    outside = box["tmp"] / "somewhere-else"
    outside.mkdir()
    outside.chmod(0o777)
    (box["zp"] / "venv").unlink()
    (box["zp"] / "venv").symlink_to(outside)
    result, calls = run(box)
    assert result.returncode == 0
    assert "resolves outside" in result.stderr
    assert stat.S_IMODE(outside.stat().st_mode) == 0o777
    assert not any(str(outside) in c for c in calls)


def test_a_machine_without_the_optional_directories_is_fine(box):
    import shutil
    shutil.rmtree(box["zp"] / "bin")
    (box["zp"] / "venv").unlink()
    result, _ = run(box)
    assert result.returncode == 0, result.stderr


def test_config_directory_prevents_module_creation_but_data_is_writable(box):
    result, calls = run(box)
    assert result.returncode == 0, result.stderr
    config = box['updater'] / 'config'
    assert stat.S_IMODE(config.stat().st_mode) == 0o755
    assert (config / 'subscription.json').read_text().strip() == '{}'
    assert stat.S_IMODE((config / 'agent.conf').stat().st_mode) == 0o600
    assert stat.S_IMODE((config / 'subscription.json').stat().st_mode) == 0o600
    assert f'chown root:root {config}' in calls


@pytest.mark.parametrize('name', ['__init__.py', '__init__.pyc', '__init__.abi3.so'])
def test_setup_refuses_a_planted_config_package(box, name):
    (box['updater'] / 'config' / name).write_text('# untrusted code\n')
    result, calls = run(box)
    assert result.returncode != 0
    assert 'Unexpected Python package' in result.stderr


def test_state_symlink_is_refused_without_touching_the_target(box):
    target = box['tmp'] / 'other-file'
    target.write_text('preserve this\n')
    state = box['updater'] / 'config' / 'subscription.json'
    state.symlink_to(target)
    result, calls = run(box)
    assert result.returncode != 0
    assert target.read_text() == 'preserve this\n'
    assert 'symlink' in result.stderr


def test_root_service_environment_is_not_app_writable(box):
    env = box['zp'] / '.env'
    env.write_text('CLICKHOUSE_PASSWORD=lab-only-placeholder\n')
    env.chmod(0o666)
    result, calls = run(box)
    assert result.returncode == 0, result.stderr
    assert f'chown root:zenplus {env}' in calls
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
