"""Refuse to run with the placeholder secrets from ``config.Settings``.

Every installer writes random secrets to ``/opt/zenplus/.env``. If that file
is missing or incomplete the settings silently fall back to the published
placeholders, which would let anyone sign session tokens or log in to the
datastores. The API checks the effective values at startup and stops instead.

``ZENPLUS_ALLOW_INSECURE_DEFAULTS=1`` is a development escape hatch only.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PLACEHOLDER = "changeme"
MIN_JWT_SECRET_LENGTH = 32
ALLOW_ENV = "ZENPLUS_ALLOW_INSECURE_DEFAULTS"


class InsecureSecretsError(RuntimeError):
    pass


def _url_password(url: str) -> str | None:
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def insecure_secrets(settings) -> list[str]:
    """Names of settings that still hold a placeholder or a weak value."""
    problems: list[str] = []
    jwt_secret = settings.JWT_SECRET or ""
    if jwt_secret == PLACEHOLDER or len(jwt_secret) < MIN_JWT_SECRET_LENGTH:
        problems.append("JWT_SECRET")
    if _url_password(settings.DATABASE_URL) == PLACEHOLDER:
        problems.append("DATABASE_URL")
    if _url_password(settings.REDIS_URL) == PLACEHOLDER:
        problems.append("REDIS_URL")
    if settings.CLICKHOUSE_PASSWORD == PLACEHOLDER:
        problems.append("CLICKHOUSE_PASSWORD")
    return problems


def enforce(settings) -> None:
    problems = insecure_secrets(settings)
    if not problems:
        return
    names = ", ".join(problems)
    if os.environ.get(ALLOW_ENV) == "1":
        logger.warning("Running with placeholder or weak secrets (%s) because %s=1", names, ALLOW_ENV)
        return
    raise InsecureSecretsError(
        f"Refusing to start: {names} still use a placeholder or weak value. "
        "Set unique secrets in /opt/zenplus/.env (JWT_SECRET needs at least "
        f"{MIN_JWT_SECRET_LENGTH} characters)."
    )
