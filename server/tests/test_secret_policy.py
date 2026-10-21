"""The API refuses to start on the published placeholder secrets (VAPT ZP-02)."""
from types import SimpleNamespace

import pytest

from app.core import secret_policy

GOOD = "a" * 64


def _settings(**overrides):
    values = dict(
        JWT_SECRET=GOOD,
        DATABASE_URL=f"postgresql+asyncpg://zenplus:{'b' * 32}@127.0.0.1:5432/zenplus",
        REDIS_URL=f"redis://:{'c' * 32}@127.0.0.1:6379/0",
        CLICKHOUSE_PASSWORD="d" * 32,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_installed_style_secrets_pass(monkeypatch):
    monkeypatch.delenv(secret_policy.ALLOW_ENV, raising=False)
    assert secret_policy.insecure_secrets(_settings()) == []
    secret_policy.enforce(_settings())


@pytest.mark.parametrize("overrides,name", [
    ({"JWT_SECRET": "changeme"}, "JWT_SECRET"),
    ({"JWT_SECRET": ""}, "JWT_SECRET"),
    ({"JWT_SECRET": "short-secret"}, "JWT_SECRET"),
    ({"DATABASE_URL": "postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus"}, "DATABASE_URL"),
    ({"REDIS_URL": "redis://:changeme@localhost:6379/0"}, "REDIS_URL"),
    ({"CLICKHOUSE_PASSWORD": "changeme"}, "CLICKHOUSE_PASSWORD"),
])
def test_placeholders_refuse_startup(monkeypatch, overrides, name):
    monkeypatch.delenv(secret_policy.ALLOW_ENV, raising=False)
    settings = _settings(**overrides)
    assert secret_policy.insecure_secrets(settings) == [name]
    with pytest.raises(secret_policy.InsecureSecretsError) as exc:
        secret_policy.enforce(settings)
    assert name in str(exc.value)


def test_config_defaults_are_all_flagged(monkeypatch):
    from app.core.config import Settings
    monkeypatch.delenv(secret_policy.ALLOW_ENV, raising=False)
    for key in ("JWT_SECRET", "DATABASE_URL", "REDIS_URL", "CLICKHOUSE_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    defaults = Settings(_env_file=None)
    assert set(secret_policy.insecure_secrets(defaults)) == {
        "JWT_SECRET", "DATABASE_URL", "REDIS_URL", "CLICKHOUSE_PASSWORD"}


def test_development_escape_hatch(monkeypatch):
    monkeypatch.setenv(secret_policy.ALLOW_ENV, "1")
    secret_policy.enforce(_settings(JWT_SECRET="changeme"))


def test_redis_url_without_password_is_not_a_placeholder(monkeypatch):
    monkeypatch.delenv(secret_policy.ALLOW_ENV, raising=False)
    assert secret_policy.insecure_secrets(_settings(REDIS_URL="redis://127.0.0.1:6379/0")) == []
