"""OS-level package management steps.

Provides ``apt_install``, ``apt_remove`` and ``apt_update`` step handlers so
that a release manifest can install or remove Debian packages (e.g. a new
SNMP daemon, a system library that a new ZenPlus feature depends on, etc.).

These steps assume the updater runs as root — which it does under
``zenplus-updater.service`` (``User=root``). All commands are run with
``DEBIAN_FRONTEND=noninteractive`` so they never block on apt prompts.

Manifest examples::

    {"type": "apt_update"}
    {"type": "apt_install", "packages": ["snmpd", "snmp"], "update_first": true}
    {"type": "apt_remove",  "packages": ["old-daemon"], "purge": true}
"""

import logging
import os
import subprocess
from typing import Iterable

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

# Packages we refuse to touch from an OTA update — removing or replacing
# these can brick the appliance.
PROTECTED_PACKAGES = frozenset({
    "systemd", "systemd-sysv", "init", "libc6", "libc-bin",
    "openssh-server", "sudo", "bash", "coreutils",
    "postgresql", "postgresql-14", "postgresql-16",
    "clickhouse-server", "clickhouse-common-static",
    "nginx", "nginx-core",
})


def _apt_env() -> dict:
    """Environment that makes apt fully non-interactive."""
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["APT_LISTCHANGES_FRONTEND"] = "none"
    env["NEEDRESTART_MODE"] = "a"
    return env


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError(
            "OS package steps require root. The zenplus-updater service must "
            "be configured with User=root (see scripts/setup-updater.sh)."
        )


def _validate_packages(packages: Iterable[str]) -> list[str]:
    """Reject empty lists, shell-metacharacters, and protected packages."""
    pkgs = [p.strip() for p in packages if p and p.strip()]
    if not pkgs:
        raise ValueError("Empty package list")

    bad_chars = set(";|&$`<>\n\r\t ")
    for p in pkgs:
        if any(c in bad_chars for c in p):
            raise ValueError(f"Invalid character in package name: {p!r}")
        # Allow upstream naming: letters, digits, '.', '+', '-', '_', ':' (arch)
        for c in p:
            if not (c.isalnum() or c in ".+-_:"):
                raise ValueError(f"Invalid character in package name: {p!r}")
        if p in PROTECTED_PACKAGES:
            raise ValueError(
                f"Refusing to manage protected package: {p}. "
                "Protected packages cannot be changed via OTA."
            )
    return pkgs


def _run_apt(args: list[str], timeout: int) -> None:
    """Run an apt-get command and raise on non-zero exit."""
    cmd = ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confdef",
           "-o", "Dpkg::Options::=--force-confold", *args]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_apt_env(),
    )
    if result.stdout.strip():
        logger.info("apt stdout: %s", result.stdout.strip()[:2000])
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"apt-get {args[0]} failed: {stderr[:2000]}")


@step_handler("apt_update")
def apt_update(step: dict, extract_dir: str, cfg) -> None:
    """Refresh the apt package index."""
    _require_root()
    _run_apt(["update"], timeout=step.get("timeout", 180))


@step_handler("apt_install")
def apt_install(step: dict, extract_dir: str, cfg) -> None:
    """Install one or more Debian packages."""
    _require_root()
    packages = _validate_packages(step.get("packages", []))

    if step.get("update_first", True):
        _run_apt(["update"], timeout=step.get("update_timeout", 180))

    install_args = ["install"]
    if step.get("no_recommends"):
        install_args.append("--no-install-recommends")
    install_args.extend(packages)

    _run_apt(install_args, timeout=step.get("timeout", 900))
    logger.info("Installed packages: %s", ", ".join(packages))


@step_handler("apt_remove")
def apt_remove(step: dict, extract_dir: str, cfg) -> None:
    """Remove (optionally purge) Debian packages."""
    _require_root()
    packages = _validate_packages(step.get("packages", []))

    action = "purge" if step.get("purge") else "remove"
    _run_apt([action, *packages], timeout=step.get("timeout", 300))

    if step.get("autoremove", True):
        _run_apt(["autoremove"], timeout=step.get("timeout", 300))

    logger.info("Removed packages: %s", ", ".join(packages))
