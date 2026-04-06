"""Start/stop/restart systemd services."""

import subprocess
import logging

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


def _service_exists(service: str) -> bool:
    """Check if a systemd unit exists on this system."""
    result = subprocess.run(
        ["systemctl", "cat", service],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def _systemctl(action: str, services: list[str]) -> None:
    """Run systemctl action on a list of services.

    - Skips services that don't exist on this system
    - Uses longer timeout for stop (services may take time to drain)
    - Uses kill as fallback if stop times out
    """
    timeout = 120 if action == "stop" else 60

    for service in services:
        if not _service_exists(service):
            logger.info("Skipping %s %s (service not found)", action, service)
            continue

        logger.info("systemctl %s %s", action, service)
        try:
            result = subprocess.run(
                ["systemctl", action, service],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                # Don't fail on "not loaded" or "not found"
                if "not found" in stderr or "not loaded" in stderr:
                    logger.warning("Service %s not found, skipping", service)
                    continue
                raise RuntimeError(
                    f"systemctl {action} {service} failed: {stderr}"
                )
        except subprocess.TimeoutExpired:
            if action == "stop":
                # Force kill if graceful stop timed out
                logger.warning(
                    "systemctl stop %s timed out, force killing ...", service
                )
                subprocess.run(
                    ["systemctl", "kill", "-s", "SIGKILL", service],
                    capture_output=True, timeout=30,
                )
            else:
                raise


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
