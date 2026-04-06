"""System inventory collector for appliance check-in."""

import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")
VERSION_FILE = ZENPLUS_DIR / ".version"

TRACKED_SERVICES = [
    "zenplus-api",
    "zenplus-poller",
    "netmon-gunicorn",
    "netmon-celery",
    "netmon-celery-beat",
    "nginx",
    "redis-server",
    "postgresql@14-main",
]


def get_current_version() -> str:
    """Read the current ZenPlus version from .version file."""
    try:
        if VERSION_FILE.exists():
            lines = VERSION_FILE.read_text().strip().splitlines()
            if lines:
                return lines[0].strip()
    except OSError:
        pass
    return "unknown"


def get_arch() -> str:
    """Get system architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    elif machine in ("aarch64", "arm64"):
        return "arm64"
    return machine


def get_os_version() -> str:
    """Get OS version string."""
    try:
        import distro
        return f"{distro.id()}-{distro.version()}"
    except ImportError:
        pass

    # Fallback: read /etc/os-release
    try:
        os_release = Path("/etc/os-release").read_text()
        info = {}
        for line in os_release.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip('"')
        os_id = info.get("ID", "linux")
        os_ver = info.get("VERSION_ID", "")
        return f"{os_id}-{os_ver}"
    except OSError:
        return f"linux-{platform.release()}"


def get_hostname() -> str:
    """Get system hostname."""
    return platform.node()


def get_uptime() -> int:
    """Get system uptime in seconds."""
    try:
        uptime_str = Path("/proc/uptime").read_text().split()[0]
        return int(float(uptime_str))
    except (OSError, ValueError, IndexError):
        return 0


def get_service_status(service: str) -> str:
    """Get systemd service status."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def get_services_status() -> dict[str, str]:
    """Get status of all tracked services."""
    return {svc: get_service_status(svc) for svc in TRACKED_SERVICES}


def get_disk_usage() -> dict[str, int]:
    """Get disk usage for /opt/zenplus in bytes."""
    import shutil
    total, used, free = shutil.disk_usage(str(ZENPLUS_DIR))
    return {"total": total, "used": used, "free": free}


def collect_inventory() -> dict:
    """Collect full system inventory for check-in."""
    return {
        "hostname": get_hostname(),
        "arch": get_arch(),
        "os_version": get_os_version(),
        "current_version": get_current_version(),
        "agent_version": "1.0.0",
        "uptime": get_uptime(),
        "services_status": get_services_status(),
        "disk": get_disk_usage(),
    }
