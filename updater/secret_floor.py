"""OTA secret floor, run by the signed ``ota-preflight.py`` hook.

From this release the API refuses to start while a secret in its effective
configuration is the published placeholder (``server/app/core/secret_policy``):
``JWT_SECRET`` empty, ``changeme`` or shorter than 32 characters, or a
``changeme`` password in ``DATABASE_URL``, ``REDIS_URL`` or
``CLICKHOUSE_PASSWORD``. An upgraded appliance must not end up with a dead API,
so before anything is stopped or replaced:

* a placeholder datastore password refuses the update, with nothing changed:
  the password is also configured inside PostgreSQL, Redis or ClickHouse, so
  it cannot be rotated safely from here. The Go poller and NetFlow collector
  (``poller/internal/config.CheckSecrets``) likewise refuse an unset, empty or
  ``changeme`` ``POSTGRES_PASSWORD``, ``CLICKHOUSE_PASSWORD`` or
  ``REDIS_PASSWORD``, so those are checked too;
* a weak or missing ``JWT_SECRET`` is replaced in ``/opt/zenplus/.env`` by a
  new random 64-hex secret. Owner, group and mode are preserved. Signed-in
  users have to sign in again once the API restarts.

Keep the checks in step with ``server/app/core/secret_policy.py``; the test
suite compares both on the same vectors. Standard library only: this runs from
the release payload, under whatever Python the installed updater uses.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

PLACEHOLDER = "changeme"
MIN_JWT_SECRET_LENGTH = 32
ALLOW_ENV = "ZENPLUS_ALLOW_INSECURE_DEFAULTS"
API_UNIT = "zenplus-api.service"
POLLER_UNIT = "zenplus-poller.service"
# poller/internal/config.DatastorePasswordEnvs: no built-in value is accepted.
POLLER_DATASTORE_ENVS = ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD")

# config.Settings defaults: what the API uses for a setting found nowhere else.
SETTING_DEFAULTS = {
    "DATABASE_URL": "postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus",
    "REDIS_URL": "redis://:changeme@localhost:6379/0",
    "CLICKHOUSE_PASSWORD": "changeme",
    "JWT_SECRET": "changeme",
}
DATASTORE_SETTINGS = ("DATABASE_URL", "REDIS_URL", "CLICKHOUSE_PASSWORD")

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class SecretFloorError(RuntimeError):
    """The update must not proceed; nothing has been changed."""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines as systemd's EnvironmentFile reads them; last one wins."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def unit_environment(unit: str = API_UNIT) -> dict[str, str]:
    """Environment= assignments of the API unit (lower precedence than .env)."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=Environment", "--value"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    values: dict[str, str] = {}
    try:
        words = shlex.split(result.stdout)
    except ValueError:
        return {}
    for word in words:
        key, sep, value = word.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def _url_password(url: str) -> str | None:
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def weak_jwt(values: dict[str, str]) -> bool:
    secret = values.get("JWT_SECRET", "") or ""
    return secret == PLACEHOLDER or len(secret) < MIN_JWT_SECRET_LENGTH


def placeholder_datastores(values: dict[str, str]) -> list[str]:
    problems = []
    if _url_password(values.get("DATABASE_URL", "")) == PLACEHOLDER:
        problems.append("DATABASE_URL")
    if _url_password(values.get("REDIS_URL", "")) == PLACEHOLDER:
        problems.append("REDIS_URL")
    if values.get("CLICKHOUSE_PASSWORD") == PLACEHOLDER:
        problems.append("CLICKHOUSE_PASSWORD")
    return problems


def poller_placeholders(values: dict[str, str]) -> list[str]:
    """Settings the Go services refuse: unset, blank or the placeholder."""
    return [name for name in POLLER_DATASTORE_ENVS
            if not (values.get(name) or "").strip() or values.get(name) == PLACEHOLDER]


def _read_regular(path: Path) -> tuple[str, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretFloorError(f"{path} is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8", errors="surrogateescape") as handle:
            fd = -1
            return handle.read(), info
    finally:
        if fd >= 0:
            os.close(fd)


def replace_jwt_secret(env_path: Path, text: str, info: os.stat_result, new_secret: str) -> None:
    """Rewrite JWT_SECRET in place, atomically, keeping owner, group and mode."""
    lines = text.splitlines(keepends=True)
    replaced = False
    output = []
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == "JWT_SECRET":
            if not replaced:
                output.append(f"JWT_SECRET={new_secret}\n")
                replaced = True
            continue  # drop later duplicates: the first line now wins alone
        output.append(line)
    if not replaced:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.append(f"JWT_SECRET={new_secret}\n")

    temp = env_path.with_name(f".{env_path.name}.secret-floor.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if (info.st_uid, info.st_gid) != (os.getuid(), os.getgid()) or os.geteuid() == 0:
            os.fchown(fd, info.st_uid, info.st_gid)
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as handle:
            fd = -1
            handle.write("".join(output))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, env_path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        dir_fd = os.open(env_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def enforce(zenplus_dir: Path, *, unit_env: dict[str, str] | None = None,
            poller_unit_env: dict[str, str] | None = None) -> str:
    """Refuse placeholder datastore passwords; rotate a weak JWT secret.

    Returns a one-line summary. Raises SecretFloorError (before any change) when
    the update must not proceed.
    """
    env_path = zenplus_dir / ".env"
    if env_path.is_symlink():
        raise SecretFloorError(f"{env_path} is a symlink; refusing to judge or rewrite it")
    if not env_path.exists():
        # Every installer writes this file. Without it there is nothing safe to
        # rewrite; the post-update health check and rollback still protect the
        # appliance.
        return f"secret floor skipped: {env_path} not found"
    text, info = _read_regular(env_path)
    file_values = parse_env(text)

    server_values: dict[str, str] = {}
    server_env = zenplus_dir / "server" / ".env"
    if server_env.is_file() and not server_env.is_symlink():
        try:
            server_values = parse_env(_read_regular(server_env)[0])
        except (OSError, SecretFloorError):
            server_values = {}
    if unit_env is None:
        unit_env = unit_environment(API_UNIT)
    if poller_unit_env is None:
        poller_unit_env = unit_environment(POLLER_UNIT)

    # Precedence as the running API sees it: settings defaults < server/.env
    # (pydantic env_file) < unit Environment= < /opt/zenplus/.env (systemd's
    # EnvironmentFile= overrides Environment=).
    effective = {**SETTING_DEFAULTS, **server_values, **unit_env, **file_values}
    # The poller reads plain environment variables and has no defaults.
    poller_effective = {**poller_unit_env, **file_values}
    # The development escape hatch applies per service, as each one reads it.
    api_allowed = effective.get(ALLOW_ENV) == "1"
    poller_allowed = poller_effective.get(ALLOW_ENV) == "1"
    if api_allowed and poller_allowed:
        return f"secret floor skipped: {ALLOW_ENV}=1"

    datastores = [] if api_allowed else placeholder_datastores(effective)
    if not poller_allowed:
        datastores += [name for name in poller_placeholders(poller_effective) if name not in datastores]
    if datastores:
        raise SecretFloorError(
            "Update refused, nothing was changed: "
            + ", ".join(datastores)
            + " are unset or still use the published placeholder 'changeme', which this "
            "release no longer starts with. Set a unique password in the datastore and "
            f"in {env_path}, restart ZenPlus, then retry the update."
        )

    if api_allowed or not weak_jwt(effective):
        return "secrets meet the release floor"
    replace_jwt_secret(env_path, text, info, secrets.token_hex(32))
    return (f"JWT_SECRET in {env_path} was missing or weak and has been replaced; "
            "users sign in again after the API restarts")
