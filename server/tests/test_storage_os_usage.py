"""Read-only measurements and configuration tests on disposable filesystem fixtures."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import pwd
import grp
import shlex
from types import SimpleNamespace

import pytest
from app.services import storage_service as svc

ROOT = Path(__file__).resolve().parents[2]


def helper(tmp_path):
    script = (ROOT / "scripts/zenplus-storage-helper").read_text()
    # The installed privileged helper has fixed paths; only this test copy is
    # redirected, so an unprivileged caller cannot choose measurement targets.
    for path in ("/var/log", "/var/cache/apt"):
        script = script.replace(path, str(tmp_path / path.lstrip("/")))
    p = tmp_path / "helper"
    p.write_text(script)
    return p


def run(p, env=None):
    return subprocess.run(["bash", str(p), "os-usage"], capture_output=True, text=True, env=env)


def allocated(path):
    return int(subprocess.check_output(["du", "-s", "--block-size=1", str(path)]).split()[0])


def test_optional_paths_absent_are_zero(tmp_path):
    r = run(helper(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == dict(journal_bytes=0, var_log_bytes=0, apt_cache_bytes=0)


def test_empty_package_globs_do_not_hide_system_logs(tmp_path):
    journal = tmp_path / "var/log/journal"
    journal.mkdir(parents=True)
    (journal / "system.journal").write_bytes(b"j" * 16384)
    (journal.parent / "syslog").write_bytes(b"s" * 32768)
    partial = tmp_path / "var/cache/apt/archives/partial"
    partial.mkdir(parents=True)
    r = run(helper(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == dict(journal_bytes=allocated(journal),
                                      var_log_bytes=allocated(journal.parent),
                                      apt_cache_bytes=allocated(partial))


def test_cache_counts_packages_partial_and_bins_only(tmp_path):
    cache = tmp_path / "var/cache/apt"
    partial = cache / "archives/partial"
    partial.mkdir(parents=True)
    package = partial.parent / "a.deb"
    package.write_bytes(b"p" * 8192)
    binary = cache / "pkgcache.bin"
    binary.write_bytes(b"b" * 16384)
    (cache / "unrelated").write_bytes(b"x" * 65536)
    (partial / "download").write_bytes(b"d" * 4096)
    r = run(helper(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["apt_cache_bytes"] == sum(map(allocated, (package, binary, partial)))
    binary.unlink()
    assert run(helper(tmp_path)).returncode == 0


def test_measurement_failure_is_not_reported_as_zero(tmp_path):
    logs = tmp_path / "var/log"
    logs.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    du = bindir / "du"
    du.write_text("#!/bin/sh\necho 'du: permission denied' >&2\nexit 1\n")
    du.chmod(0o755)
    r = run(helper(tmp_path), dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"))
    assert r.returncode != 0 and not r.stdout.strip()


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout=""),
    SimpleNamespace(returncode=0, stdout="not json"),
    SimpleNamespace(returncode=0, stdout='{"journal_bytes":0}'),
    SimpleNamespace(returncode=0, stdout='{"journal_bytes":-1,"var_log_bytes":1,"apt_cache_bytes":0}'),
    SimpleNamespace(returncode=0, stdout='{"journal_bytes":true,"var_log_bytes":1,"apt_cache_bytes":0}'),
])
def test_api_reports_unknown_measurements_and_a_warning(monkeypatch, result):
    monkeypatch.setattr(svc, "_run_helper", lambda *a, **k: result)
    extras = svc._os_usage_extras()
    assert extras["os_usage_error"]
    assert all(extras[k] is None for k in ("journal_bytes", "var_log_bytes", "apt_cache_bytes"))


def test_api_preserves_measured_zero(monkeypatch):
    measured = dict(journal_bytes=4096, var_log_bytes=8192, apt_cache_bytes=0)
    monkeypatch.setattr(svc, "_run_helper", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(measured)))
    assert svc._os_usage_extras() == dict(measured, os_usage_error=None)


def test_api_measurement_timeout_is_visible(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("helper", 60)
    monkeypatch.setattr(svc, "_run_helper", timeout)
    assert svc._os_usage_extras()["os_usage_error"]


@pytest.mark.asyncio
async def test_unknown_os_consumers_stay_unclassified(monkeypatch):
    class DB:
        async def execute(self, *a):
            return SimpleNamespace(scalar=lambda: 0)
    monkeypatch.setattr(svc, "_mount_of", lambda p: "/")
    rows = await svc.space_consumers(DB(), {}, {"os_usage_error": "unavailable"})
    assert not {"journal", "os_logs", "apt_cache"}.intersection(r["id"] for r in rows)


def test_logrotate_size_cap_uses_global_configuration_and_rolls_back_errors(tmp_path):
    """Run the actual setup stanza with fake logrotate; never touch /etc."""
    setup = (ROOT / "scripts/setup-storage.sh").read_text()
    start = setup.index('if [ -f /etc/logrotate.d/rsyslog ]')
    block = setup[start:setup.index('# maxsize is only checked', start)]
    etc = tmp_path / "etc"
    d = etc / "logrotate.d"
    d.mkdir(parents=True)
    conf = d / "rsyslog"
    original = "/var/log/syslog {\n    rotate 4\n    weekly\n}\n"
    global_conf = etc / "logrotate.conf"
    global_conf.write_text(f"su root adm\ninclude {d}\n")
    stub = tmp_path / "bin"
    stub.mkdir()
    fake = stub / "logrotate"
    fake.write_text(f'#!/bin/bash\n[ "$*" = "-d {global_conf}" ] || exit 99\nexit "${{TEST_ROTATE_RC:-0}}"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}")
    script = 'set -e\nlog() { :; }\nwarn() { :; }\n' + block.replace("/etc/", str(etc) + "/").replace("/var/tmp/", str(tmp_path) + "/")
    conf.write_text(original)
    assert subprocess.run(["bash", "-c", script], env=env).returncode == 0
    assert "maxsize 500M" in conf.read_text()
    before = conf.read_bytes()
    assert subprocess.run(["bash", "-c", script], env=env).returncode == 0
    assert conf.read_bytes() == before
    conf.write_text(original)
    assert subprocess.run(["bash", "-c", script], env=dict(env, TEST_ROTATE_RC="1")).returncode == 0
    assert conf.read_text() == original
    assert not (d / ".rsyslog.zenplus-bak").exists()
    assert not list(tmp_path.glob("zenplus-rsyslog.*"))


@pytest.mark.skipif(not shutil.which("logrotate"), reason="real logrotate parser unavailable")
def test_full_logrotate_include_directory_accepts_installer_backup(tmp_path):
    """Parse the actual edited stanza and include directory, including backups."""
    setup = (ROOT / "scripts/setup-storage.sh").read_text()
    start = setup.index('if [ -f /etc/logrotate.d/rsyslog ]')
    block = setup[start:setup.index('# maxsize is only checked', start)]
    etc = tmp_path / "etc"
    confdir = etc / "logrotate.d"
    confdir.mkdir(parents=True)
    conf = confdir / "rsyslog"
    log = tmp_path / "missing-syslog"
    original = f"{log} {{\n    rotate 4\n    weekly\n    missingok\n}}\n"
    conf.write_text(original)
    user, group = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
    (etc / "logrotate.conf").write_text(f"su {user} {group}\ninclude {confdir}\n")
    # Isolate state and use the current uid/gid: the test does not need sudo.
    # A separate root fixture proves Ubuntu's real `su root adm` context.
    real_rotate = shlex.quote(shutil.which("logrotate"))
    state = shlex.quote(str(tmp_path / "state"))
    script = ('set -e\nlog() { :; }\nwarn() { :; }\n'
              + f'logrotate() {{ {real_rotate} -s {state} "$@"; }}\n'
              + block.replace("/etc/", str(etc) + "/").replace("/var/tmp/", str(tmp_path) + "/"))
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "maxsize 500M" in conf.read_text(), "validation rejected the edit and restored the original"
    assert not list(tmp_path.glob("zenplus-rsyslog.*"))
    assert sorted(p.name for p in confdir.iterdir()) == ["rsyslog"]
    # Negative control: the previously used dot-prefixed backup is included.
    (confdir / ".rsyslog.zenplus-bak").write_text(original)
    rejected = subprocess.run(["logrotate", "-d", "-s", str(tmp_path / "state"), str(etc / "logrotate.conf")], capture_output=True, text=True)
    assert rejected.returncode != 0 and "duplicate log entry" in rejected.stderr
