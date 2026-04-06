"""Post-update health checks."""

import logging
import time

import httpx

logger = logging.getLogger("zenplus.updater")


class HealthCheckError(Exception):
    """Raised when a health check fails."""


def check_http(url: str, timeout: int = 30, retries: int = 5, delay: int = 3) -> bool:
    """Check if an HTTP endpoint returns 2xx.

    Retries with delay to allow services to start up.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if 200 <= resp.status_code < 300:
                logger.info("Health check passed: %s (status %d)", url, resp.status_code)
                return True
            logger.warning(
                "Health check attempt %d/%d: %s returned %d",
                attempt,
                retries,
                url,
                resp.status_code,
            )
        except httpx.RequestError as e:
            logger.warning(
                "Health check attempt %d/%d: %s — %s",
                attempt,
                retries,
                url,
                e,
            )

        if attempt < retries:
            time.sleep(delay)

    raise HealthCheckError(f"Health check failed after {retries} attempts: {url}")


def check_service_active(service: str) -> bool:
    """Check if a systemd service is active."""
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip() == "active":
            return True
        raise HealthCheckError(
            f"Service {service} is {result.stdout.strip()}, expected active"
        )
    except subprocess.TimeoutExpired:
        raise HealthCheckError(f"Timed out checking service {service}")


def run_health_checks(checks: list[dict]) -> None:
    """Run a list of health check definitions from the manifest.

    Each check dict has:
      - type: "http" or "service"
      - url: (for http) the URL to check
      - service: (for service) the systemd unit name
      - timeout: seconds to wait
    """
    for check in checks:
        check_type = check.get("type", "http")
        if check_type == "http":
            url = check["url"]
            timeout = check.get("timeout", 30)
            check_http(url, timeout=timeout)
        elif check_type == "service":
            check_service_active(check["service"])
        else:
            logger.warning("Unknown health check type: %s", check_type)
