"""OTA secret floor (ZP-02, updater side).

The API now refuses placeholder secrets at startup. The signed preflight hook
must therefore refuse an appliance with a placeholder datastore password before
anything changes, and replace a weak JWT secret in /opt/zenplus/.env (owner,
group and mode kept) so the upgraded API starts.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater import secret_floor

ROOT = Path(__file__).resolve().parents[2]
GOOD_JWT = "a" * 64
GOOD = {
    "JWT_SECRET": GOOD_JWT,
    "DATABASE_URL": "postgresql+asyncpg://zenplus:0123456789abcdef@localhost:5432/zenplus",
    "REDIS_URL": "redis://:fedcba9876543210@localhost:6379/0",
    "CLICKHOUSE_PASSWORD": "0011223344556677",
}


POLLER_GOOD = {"POSTGRES_PASSWORD": "0123456789abcdef", "REDIS_PASSWORD": "fedcba9876543210"}


def env_text(**overrides) -> str:
    values = {**POLLER_GOOD, **GOOD, **overrides}
    lines = ["# ZenPlus Configuration"]
    lines += [f"{k}={v}" for k, v in values.items() if v is not None]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def no_live_units(monkeypatch):
    """Never read the installed systemd units from a test."""
    monkeypatch.setattr(secret_floor, "unit_environment", lambda unit=None: {})


@pytest.fixture
def appliance(tmp_path):
    (tmp_path / ".env").write_text(env_text())
    (tmp_path / ".env").chmod(0o640)
    return tmp_path


VECTORS = [
    ({}, []),
    ({"JWT_SECRET": "changeme"}, ["JWT_SECRET"]),
    ({"JWT_SECRET": ""}, ["JWT_SECRET"]),
    ({"JWT_SECRET": "jwt_1700000000_secret"}, ["JWT_SECRET"]),  # old timestamp fallback
    ({"JWT_SECRET": "x" * 31}, ["JWT_SECRET"]),
    ({"JWT_SECRET": "x" * 32}, []),
    ({"DATABASE_URL": "postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus"}, ["DATABASE_URL"]),
    ({"REDIS_URL": "redis://:changeme@localhost:6379/0"}, ["REDIS_URL"]),
    ({"REDIS_URL": "redis://localhost:6379/0"}, []),
    ({"CLICKHOUSE_PASSWORD": "changeme"}, ["CLICKHOUSE_PASSWORD"]),
    ({"CLICKHOUSE_PASSWORD": "changeme2"}, []),
]


@pytest.mark.parametrize("overrides,expected", VECTORS)
def test_floor_matches_the_api_startup_policy(overrides, expected):
    from app.core import secret_policy
    values = {**GOOD, **overrides}
    assert secret_policy.insecure_secrets(SimpleNamespace(**values)) == expected
    found = (["JWT_SECRET"] if secret_floor.weak_jwt(values) else []) + secret_floor.placeholder_datastores(values)
    assert found == expected
    assert secret_floor.PLACEHOLDER == secret_policy.PLACEHOLDER
    assert secret_floor.MIN_JWT_SECRET_LENGTH == secret_policy.MIN_JWT_SECRET_LENGTH


def test_defaults_match_the_settings_class():
    from app.core.config import Settings
    for key, value in secret_floor.SETTING_DEFAULTS.items():
        assert Settings.model_fields[key].default == value


def test_good_secrets_leave_the_file_alone(appliance):
    before = (appliance / ".env").read_bytes()
    assert secret_floor.enforce(appliance, unit_env={}) == "secrets meet the release floor"
    assert (appliance / ".env").read_bytes() == before


@pytest.mark.parametrize("jwt", ["changeme", "", "short", None])
def test_a_weak_or_missing_jwt_secret_is_replaced_in_place(appliance, jwt):
    env = appliance / ".env"
    env.write_text(env_text(JWT_SECRET=jwt) + "export EXTRA='kept'\n")
    env.chmod(0o640)
    info = env.stat()
    message = secret_floor.enforce(appliance, unit_env={})
    assert "has been replaced" in message
    values = secret_floor.parse_env(env.read_text())
    assert len(values["JWT_SECRET"]) == 64 and int(values["JWT_SECRET"], 16) >= 0
    assert values["JWT_SECRET"] != GOOD_JWT
    for key in ("DATABASE_URL", "REDIS_URL", "CLICKHOUSE_PASSWORD", "POSTGRES_PASSWORD", "EXTRA"):
        assert key in values
    assert env.read_text().startswith("# ZenPlus Configuration\n")
    after = env.stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert (after.st_uid, after.st_gid) == (info.st_uid, info.st_gid)
    assert not [p for p in appliance.iterdir() if p.name.startswith(".env.") or ".tmp" in p.name]
    # Idempotent: a second run keeps the new secret.
    secret = values["JWT_SECRET"]
    assert secret_floor.enforce(appliance, unit_env={}) == "secrets meet the release floor"
    assert secret_floor.parse_env(env.read_text())["JWT_SECRET"] == secret


def test_duplicate_assignments_collapse_to_one_strong_secret(appliance):
    env = appliance / ".env"
    env.write_text(env_text(JWT_SECRET="changeme") + "JWT_SECRET=also-weak\n")
    secret_floor.enforce(appliance, unit_env={})
    lines = [l for l in env.read_text().splitlines() if l.startswith("JWT_SECRET=")]
    assert len(lines) == 1 and len(lines[0].split("=", 1)[1]) == 64


@pytest.mark.parametrize("key,value", [
    ("DATABASE_URL", "postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus"),
    ("REDIS_URL", "redis://:changeme@localhost:6379/0"),
    ("CLICKHOUSE_PASSWORD", "changeme"),
    ("CLICKHOUSE_PASSWORD", None),  # missing: the API falls back to the placeholder
])
def test_a_placeholder_datastore_password_refuses_before_any_change(appliance, key, value):
    env = appliance / ".env"
    env.write_text(env_text(**{key: value, "JWT_SECRET": "changeme"}))
    before = env.read_bytes()
    with pytest.raises(secret_floor.SecretFloorError) as refused:
        secret_floor.enforce(appliance, unit_env={})
    assert key in str(refused.value) and "nothing was changed" in str(refused.value)
    assert env.read_bytes() == before, "the JWT must not be rotated on a refused update"


@pytest.mark.parametrize("key,value", [
    ("POSTGRES_PASSWORD", None), ("POSTGRES_PASSWORD", "changeme"), ("POSTGRES_PASSWORD", " "),
    ("REDIS_PASSWORD", None), ("REDIS_PASSWORD", ""),
])
def test_go_services_datastore_passwords_are_checked_too(appliance, key, value):
    """poller/internal/config.CheckSecrets refuses unset, blank or 'changeme'."""
    env = appliance / ".env"
    env.write_text(env_text(**{key: value}))
    before = env.read_bytes()
    with pytest.raises(secret_floor.SecretFloorError, match=key):
        secret_floor.enforce(appliance, unit_env={}, poller_unit_env={})
    assert env.read_bytes() == before
    if value is None:
        # Absent from .env: the poller unit's own Environment= is what it sees.
        assert secret_floor.enforce(appliance, unit_env={}, poller_unit_env={key: "fromunit"}) \
            == "secrets meet the release floor"


def test_go_policy_names_match_the_poller_source():
    source = (ROOT / "poller" / "internal" / "config" / "config.go").read_text()
    if "DatastorePasswordEnvs" not in source:
        pytest.skip("poller secret check not present in this tree")
    listed = source.split("DatastorePasswordEnvs = []string{", 1)[1].split("}", 1)[0]
    assert tuple(n.strip().strip('"') for n in listed.split(",")) == secret_floor.POLLER_DATASTORE_ENVS


def test_effective_values_follow_systemd_precedence(appliance):
    env = appliance / ".env"
    env.write_text(env_text(CLICKHOUSE_PASSWORD=None, JWT_SECRET=None))
    # Unit Environment= fills in what .env lacks ...
    unit = {"CLICKHOUSE_PASSWORD": "fromunit123", "JWT_SECRET": "u" * 40}
    with pytest.raises(secret_floor.SecretFloorError, match="CLICKHOUSE_PASSWORD"):
        secret_floor.enforce(appliance, unit_env=unit, poller_unit_env={})  # poller would refuse
    assert secret_floor.enforce(appliance, unit_env=unit, poller_unit_env={"CLICKHOUSE_PASSWORD": "p" * 12}) \
        == "secrets meet the release floor"
    # ... but .env (EnvironmentFile=) wins over it.
    env.write_text(env_text())
    assert secret_floor.enforce(appliance, unit_env={"CLICKHOUSE_PASSWORD": "changeme"}) \
        == "secrets meet the release floor"


def test_escape_hatch_and_missing_or_unsafe_env(appliance, tmp_path):
    env = appliance / ".env"
    env.write_text(env_text(CLICKHOUSE_PASSWORD="changeme") + "ZENPLUS_ALLOW_INSECURE_DEFAULTS=1\n")
    assert "skipped" in secret_floor.enforce(appliance, unit_env={})
    env.unlink()
    assert "not found" in secret_floor.enforce(appliance, unit_env={})
    real = tmp_path / "real.env"
    real.write_text(env_text(JWT_SECRET="changeme"))
    env.symlink_to(real)
    with pytest.raises(secret_floor.SecretFloorError, match="symlink"):
        secret_floor.enforce(appliance, unit_env={})
    assert "changeme" in real.read_text()


def test_parse_env_matches_environmentfile_quoting():
    values = secret_floor.parse_env(
        "# c\n; c\nA=1\nexport B = two\nC='three'\nD=\"four\"\n bad line\nA=last\n")
    assert values == {"A": "last", "B": "two", "C": "three", "D": "four"}


# ── the signed preflight hook ────────────────────────────────────────────────

def run_preflight(tmp_path, installed: Path):
    package = tmp_path / "package"
    if not package.exists():
        (package / "code" / "scripts").mkdir(parents=True)
        shutil.copytree(ROOT / "updater", package / "code" / "updater",
                        ignore=shutil.ignore_patterns("__pycache__", "config"))
        shutil.copy2(ROOT / "scripts" / "ota-preflight.py", package / "code" / "scripts")
        (package / "manifest.json").write_text(json.dumps(
            dict(version="1.23.10", min_version="1.23.9", update_id="fixture-update")))
        (package / "code" / ".version").write_text("1.23.10")
    launcher = f'''
import pathlib, runpy, sys
sys.dont_write_bytecode = True
sys.path.insert(0, {str(package / 'code')!r})
from updater import backup_transaction as transaction, secret_floor
from updater.trust import require_root_owned
base = pathlib.Path({str(tmp_path)!r})
transaction.STATE = base/'state'
transaction.BACKUPS = transaction.STATE/'backups'
transaction.TRANSACTIONS = transaction.STATE/'transactions'
def checked(path, **kw):
    require_root_owned(path, trusted_uid=base.stat().st_uid, stop_at=base, **kw)
transaction.require_root_owned = checked
secret_floor.unit_environment = lambda unit=None: {{}}
runpy.run_path({str(package / 'code/scripts/ota-preflight.py')!r}, run_name='__main__')
'''
    return subprocess.run([sys.executable, "-c", launcher], capture_output=True, text=True,
                          env={**os.environ, "ZENPLUS_DIR": str(installed)}, timeout=60)


def test_preflight_refuses_placeholder_datastores_without_starting_a_transaction(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / ".version").write_text("1.23.9")
    (installed / ".env").write_text(env_text(REDIS_URL="redis://:changeme@localhost:6379/0",
                                             JWT_SECRET="changeme"))
    before = (installed / ".env").read_bytes()
    result = run_preflight(tmp_path, installed)
    assert result.returncode != 0
    assert "REDIS_URL" in result.stderr and "nothing was changed" in result.stderr
    assert (installed / ".env").read_bytes() == before
    assert not (tmp_path / "state" / "transactions").exists() or \
        not any((tmp_path / "state" / "transactions").iterdir())


def test_preflight_rotates_a_weak_jwt_and_proceeds(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / ".version").write_text("1.23.9")
    (installed / ".env").write_text(env_text(JWT_SECRET="jwt_1700000000_secret"))
    result = run_preflight(tmp_path, installed)
    assert result.returncode == 0, result.stderr
    assert "has been replaced" in result.stdout and "Prerequisite verified" in result.stdout
    assert len(secret_floor.parse_env((installed / ".env").read_text())["JWT_SECRET"]) == 64
    assert not list((tmp_path / "package" / "code").rglob("__pycache__"))


def test_the_floor_runs_inside_the_first_manifest_step():
    preflight = (ROOT / "scripts" / "ota-preflight.py").read_text()
    assert preflight.index("validate_transition(manifest, current)") \
        < preflight.index("enforce(installed)") < preflight.index("begin_transaction(")
    build = (ROOT / "scripts" / "build-release.py").read_text()
    assert 'steps = [{"type": "run_hook", "script": "code/scripts/ota-preflight.py"' in build
