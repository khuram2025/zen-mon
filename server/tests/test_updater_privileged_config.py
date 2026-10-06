"""Regression checks for app-writable data crossing the root updater boundary."""
import logging
import os
from pathlib import Path
import py_compile
import shlex
import shutil
import subprocess
import sys
import pytest

from updater.config import AgentConfig, load_config, save_config
from updater.crypto import SecurityError
from updater.trust import TrustSettings, require_root_owned

ROOT = Path(__file__).resolve().parents[2]


def test_app_config_cannot_redirect_privileged_files(tmp_path, caplog):
    conf = tmp_path / 'agent.conf'
    conf.write_text('[update]\nbackup_dir = /tmp/app-controlled\nmax_backups = 0\n'
                    '[logging]\nlog_file = /tmp/target-owned-by-root\n'
                    'max_log_size_mb = 1\nlog_rotate_count = 0\n')
    with caplog.at_level(logging.WARNING, logger='zenplus.updater'):
        cfg = load_config(str(conf), trust=TrustSettings())
    defaults = AgentConfig()
    assert cfg.update.backup_dir == defaults.update.backup_dir
    assert cfg.update.max_backups == defaults.update.max_backups
    assert cfg.logging.log_file == defaults.logging.log_file
    assert cfg.logging.max_log_size_mb == defaults.logging.max_log_size_mb
    assert cfg.logging.log_rotate_count == defaults.logging.log_rotate_count
    assert 'privileged path' in caplog.text


def test_privileged_file_policy_is_not_written_back_to_app_state(tmp_path):
    path = tmp_path / 'agent.conf'
    save_config(AgentConfig(), str(path))
    text = path.read_text()
    for name in ('backup_dir', 'max_backups', 'log_file', 'max_log_size_mb', 'log_rotate_count'):
        assert name not in text


@pytest.mark.parametrize('name', ['__init__.py', '__init__.pyc', '__init__.abi3.so'])
def test_module_planting_is_rejected_before_the_config_module_import(tmp_path, name):
    shutil.copytree(ROOT / 'updater', tmp_path / 'updater', ignore=shutil.ignore_patterns('__pycache__'))
    conf = tmp_path / 'updater' / 'config'
    conf.mkdir(exist_ok=True)
    marker = tmp_path / 'unexpected-import'
    source = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n'
    if name.endswith('.pyc'):
        planted_source = tmp_path / 'planted.py'
        planted_source.write_text(source)
        py_compile.compile(str(planted_source), cfile=str(conf / name), doraise=True)
    else:
        (conf / name).write_text(source)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'updater', '--help'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert not marker.exists(), 'App-owned config/__init__.py was executed before the trust checks'
    assert 'unexpected Python package' in result.stderr


def test_empty_precreated_subscription_cache_is_not_a_license(tmp_path, monkeypatch):
    from app.api.v1 import system_updates
    cache = tmp_path / 'subscription.json'
    cache.write_text('{}\n')
    monkeypatch.setattr(system_updates, 'SUBSCRIPTION_PATH', cache)
    assert system_updates._load_subscription() is None


def test_trusted_leaf_cannot_hide_a_symlinked_parent(tmp_path):
    real = tmp_path / 'root-owned'
    real.mkdir(mode=0o755)
    (real / 'trust.conf').write_text('[trust]\n')
    (real / 'trust.conf').chmod(0o644)
    alias = tmp_path / 'replaceable-alias'
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(SecurityError, match='symlink'):
        require_root_owned(alias / 'trust.conf', what='trust file',
                           trusted_uid=os.getuid(), stop_at=tmp_path)


def test_trust_boundary_must_be_an_ancestor(tmp_path):
    path = tmp_path / 'trust.conf'
    path.write_text('[trust]\n')
    with pytest.raises(SecurityError, match='outside trust boundary'):
        require_root_owned(path, what='trust file', trusted_uid=os.getuid(),
                           stop_at=tmp_path / 'unrelated')


def test_installer_stops_before_finalization_when_updater_setup_fails(tmp_path):
    # Keep the real step framework/main, but replace system-changing phases.
    definitions = tmp_path / 'installer-definitions.sh'
    definitions.write_text((ROOT / 'install.sh').read_text().rsplit('\nmain "$@"', 1)[0])
    marker = tmp_path / 'continued-after-failure'
    harmless = ('check_root show_banner preflight_checks install_prerequisites '
                'setup_user fetch_code configure_env build_components setup_databases '
                'seed_trial_licence create_services setup_security_hardening '
                'create_cli finalize verify_installation collect_report print_summary')
    script = f'source {shlex.quote(str(definitions))}\n'
    script += ''.join(f'{name}() {{ :; }}\n' for name in harmless.split())
    script += 'setup_updater() { return 1; }\n'
    script += f'setup_support_bundles() {{ touch {shlex.quote(str(marker))}; }}\nmain\n'
    env = dict(os.environ, INSTALL_LOG=str(tmp_path / 'install.log'))
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode != 0
    assert not marker.exists(), 'Installer continued after failed privileged updater setup'
    assert 'Configuring OTA updater' in result.stdout
