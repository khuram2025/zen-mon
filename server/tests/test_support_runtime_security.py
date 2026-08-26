from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_support_dispatch_service_is_unprivileged_and_hardened():
    unit = _text("updater/systemd/zenplus-support-dispatch.service")
    assert "User=zenplus" in unit
    assert "Group=zenplus" in unit
    assert "User=root" not in unit
    assert "ExecStart=/usr/local/libexec/zenplus-support-dispatch" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "AmbientCapabilities=\n" in unit
    assert "InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock" in unit
    assert "ReadWritePaths=/opt/zenplus/support/requests " in unit


def test_support_cleanup_is_unprivileged_and_cannot_use_network_or_devices():
    unit = _text("updater/systemd/zenplus-support-cleanup.service")
    assert "User=zenplus" in unit
    assert "Group=zenplus" in unit
    assert "User=root" not in unit
    assert "PrivateNetwork=true" in unit
    assert "PrivateDevices=true" in unit
    assert "NoNewPrivileges=true" in unit


def test_queue_path_replaces_legacy_root_template():
    queue = _text("updater/systemd/zenplus-support-queue.path")
    assert "PathExistsGlob=/opt/zenplus/support/requests/*.json" in queue
    assert "Unit=zenplus-support-dispatch.service" in queue
    assert not (REPO_ROOT / "updater/systemd/zenplus-support-bundle@.service").exists()


def test_dispatcher_accepts_only_owned_regular_uuid_requests():
    dispatcher = _text("scripts/zenplus-support-dispatch")
    assert "[ -L \"${request_path}\" ]" in dispatcher
    assert "[ ! -f \"${request_path}\" ]" in dispatcher
    assert 'stat -c %U -- "${request_path}"' in dispatcher
    assert "^[0-9a-f]{8}-[0-9a-f]{4}" in dispatcher
    assert 'python -m support --job "${job_id}"' in dispatcher
    commands = "\n".join(
        line for line in dispatcher.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert "sudo" not in commands
    assert "systemctl" not in commands


def test_setup_removes_all_legacy_privilege_grants_and_rejects_symlink_dirs():
    setup = _text("scripts/setup-support.sh")
    for legacy in (
        "/etc/systemd/system/zenplus-support-bundle@.service",
        "/etc/sudoers.d/zenplus-support",
        "/etc/polkit-1/rules.d/51-zenplus-support.rules",
        "/etc/polkit-1/localauthority/50-local.d/zenplus-support.pkla",
    ):
        assert legacy in setup
    assert 'if [ -L "${runtime_dir}" ]' in setup
    assert "systemctl enable --now zenplus-support-queue.path" in setup
    assert "ZENPLUS_SOURCE_DIR" in setup
    assert "visudo" not in setup
    assert "pkaction" not in setup


def test_release_and_rollback_both_enforce_secure_support_runtime():
    builder = _text("scripts/build-release.py")
    setup_hook = '"script": "code/scripts/setup-support.sh"'
    floor_hook = '"script": "code/scripts/enforce-support-security-floor.sh"'
    assert setup_hook in builder
    assert floor_hook in builder
    assert builder.index(setup_hook) < builder.index('"rollback_steps"')
    assert builder.index('"rollback_steps"') < builder.index(floor_hook)


def test_fresh_install_treats_support_security_setup_as_required():
    installer = _text("install.sh")
    assert 'run_step      "Installing support tooling"   setup_support_bundles' in installer
    assert 'run_soft_step "Installing support tooling"   setup_support_bundles' not in installer
