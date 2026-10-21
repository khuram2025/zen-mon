"""install.sh secret generation fails closed (N6) and keeps existing credentials.

Secrets used to be generated at the top of the script, before prerequisites
were installed, with `date +%s` as the fallback. They are now generated in
configure_env from a CSPRNG (openssl, python3 secrets, /dev/urandom) or the
install stops.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def definitions(tmp_path: Path) -> Path:
    path = tmp_path / "installer-definitions.sh"
    path.write_text(INSTALL.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0])
    return path


def bash(tmp_path, body: str, *, path_prefix: Path | None = None):
    env = dict(os.environ, INSTALL_LOG=str(tmp_path / "install.log"))
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    script = f"source {shlex.quote(str(definitions(tmp_path)))}\n{body}"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def failing_stubs(tmp_path, names):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in names:
        (stubs / name).write_text("#!/bin/sh\nexit 1\n")
        (stubs / name).chmod(0o755)
    return stubs


def test_no_predictable_fallback_remains():
    text = INSTALL.read_text()
    assert "date +%s" not in text
    top = text[:text.index("# ─── Presentation")]
    assert "openssl rand" not in top, "secrets must not be generated before prerequisites"


@pytest.mark.parametrize("broken,source", [
    ([], "openssl"),
    (["openssl"], "python3"),
    (["openssl", "python3"], "/dev/urandom"),
])
def test_random_hex_uses_a_csprng_source(tmp_path, broken, source):
    stubs = failing_stubs(tmp_path, broken)
    result = bash(tmp_path, "random_hex 16; random_hex 32", path_prefix=stubs)
    assert result.returncode == 0, result.stderr
    first, second = result.stdout.split()
    assert re.fullmatch(r"[0-9a-f]{32}", first) and re.fullmatch(r"[0-9a-f]{64}", second)


def test_random_hex_fails_closed_when_no_source_works(tmp_path):
    stubs = failing_stubs(tmp_path, ["openssl", "python3", "od"])
    result = bash(tmp_path, 'value=$(random_hex 16) && echo "got:$value" || echo "failed rc=$?"',
                  path_prefix=stubs)
    assert "failed rc=1" in result.stdout and "got:" not in result.stdout


def run_configure_env(tmp_path, existing: str | None, *, stubs: Path | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if existing is not None:
        (home / ".env").write_text(existing)
    body = f"""
ZENPLUS_HOME={shlex.quote(str(home))}
chown() {{ :; }}
configure_env
echo "rc=$?"
"""
    return bash(tmp_path, body, path_prefix=stubs), home / ".env"


def parse(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if line and not line.startswith("#"))


def test_fresh_configuration_gets_strong_unique_secrets(tmp_path):
    result, env = run_configure_env(tmp_path, None)
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    values = parse(env)
    assert len(values["JWT_SECRET"]) == 64
    for key in ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[0-9a-f]{32}", values[key]), key
    assert len(values["SNMP_ENC_KEY"]) == 64
    assert len({values[k] for k in ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD")}) == 3
    assert values["DATABASE_URL"].endswith(f":{values['POSTGRES_PASSWORD']}@localhost:5432/zenplus")
    assert "API_HOST" not in values, "the API listen address is not configurable"
    assert (env.stat().st_mode & 0o777) == 0o640


def test_rerun_keeps_datastore_passwords_and_replaces_a_weak_jwt(tmp_path):
    existing = ("POSTGRES_PASSWORD=zenplus_1700000000\nCLICKHOUSE_PASSWORD=chpass\n"
                "REDIS_PASSWORD=rdpass\nJWT_SECRET=jwt_1700000000_secret\nSNMP_ENC_KEY=" + "b" * 64 + "\n")
    result, env = run_configure_env(tmp_path, existing)
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    values = parse(env)
    assert values["POSTGRES_PASSWORD"] == "zenplus_1700000000"
    assert values["CLICKHOUSE_PASSWORD"] == "chpass" and values["REDIS_PASSWORD"] == "rdpass"
    assert values["SNMP_ENC_KEY"] == "b" * 64
    assert len(values["JWT_SECRET"]) == 64 and values["JWT_SECRET"] != "jwt_1700000000_secret"


def test_rerun_keeps_a_strong_jwt(tmp_path):
    jwt = "c" * 64
    existing = f"POSTGRES_PASSWORD=p\nCLICKHOUSE_PASSWORD=c\nREDIS_PASSWORD=r\nJWT_SECRET={jwt}\nSNMP_ENC_KEY={'d' * 64}\n"
    result, env = run_configure_env(tmp_path, existing)
    assert parse(env)["JWT_SECRET"] == jwt


def test_configuration_stops_when_no_secret_can_be_generated(tmp_path):
    stubs = failing_stubs(tmp_path, ["openssl", "python3", "od"])
    result, env = run_configure_env(tmp_path, None, stubs=stubs)
    assert "rc=0" not in result.stdout
    assert "Could not generate secrets" in result.stdout
    assert not env.exists()
