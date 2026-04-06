"""Start/stop/restart systemd services."""

import subprocess
import logging

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


def _systemctl(action: str, services: list[str]) -> None:
    """Run systemctl action on a list of services."""
    for service in services:
        logger.info("systemctl %s %s", action, service)
        result = subprocess.run(
            ["systemctl", action, service],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl {action} {service} failed: {result.stderr.strip()}"
            )


@step_handler("stop_services")
def stop_services(step: dict, extract_dir: str, cfg) -> None:
    services = step.get("services", [])
    _systemctl("stop", services)


@step_handler("start_services")
def start_services(step: dict, extract_dir: str, cfg) -> None:
    services = step.get("services", [])
    _systemctl("start", services)


@step_handler("restart_services")
def restart_services(step: dict, extract_dir: str, cfg) -> None:
    services = step.get("services", [])
    _systemctl("restart", services)
