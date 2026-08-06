"""System inventory collector for appliance check-in."""

import logging
import os
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")
VERSION_FILE = ZENPLUS_DIR / ".version"

TRACKED_SERVICES = [
    "zenplus-api",
    "zenplus-poller",
    "zenplus-dashboard",
    "nginx",
    "redis-server",
    "postgresql",
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


def get_node_count() -> int:
    """Local node count = devices + service_checks.

    Reports 0 on any DB failure. The license panel on zentryc.com is purely
    informational, so a transient query error must never abort the check-in.
    DATABASE_URL is loaded from the same .env the API service uses; the
    systemd unit imports it via EnvironmentFile.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return 0
    # SQLAlchemy-style "postgresql+asyncpg://" → asyncpg accepts "postgresql://"
    asyncpg_url = db_url.replace("+asyncpg", "", 1)

    async def _query() -> int:
        import asyncpg
        conn = await asyncpg.connect(asyncpg_url)
        try:
            row = await conn.fetchval(
                "SELECT (SELECT COUNT(*) FROM devices) "
                "+ (SELECT COUNT(*) FROM service_checks)"
            )
            return int(row or 0)
        finally:
            await conn.close()

    try:
        import asyncio
        return asyncio.run(_query())
    except Exception as e:
        logger.warning("node_count query failed: %s", e)
        return 0


def get_schema_status() -> dict:
    """Report whether this appliance's schema matches its code.

    Written by scripts/sync-schema.py on every update. Reported at check-in so
    a drifting appliance is visible from the fleet view instead of only from
    its own logs — two appliances on the same version number are not
    necessarily running the same thing, and that was invisible before.
    """
    status_file = ZENPLUS_DIR / ".schema-status.json"
    if not status_file.exists():
        return {"ok": None, "reason": "never checked"}
    try:
        import json
        data = json.loads(status_file.read_text())
    except (OSError, ValueError) as e:
        return {"ok": None, "reason": f"unreadable: {e}"}

    problems = data.get("problems", []) or []
    return {
        "ok": bool(data.get("ok")),
        "checked_at": data.get("checked_at", ""),
        "problem_count": len(problems),
        "problems": problems[:20],
    }


def get_dashboard_build() -> str:
    """Fingerprint the dashboard bundle actually being served.

    Two appliances reporting the same version served different JS bundles, and
    nothing in the check-in payload could have revealed it.
    """
    assets = ZENPLUS_DIR / "dashboard" / "dist" / "assets"
    if not assets.is_dir():
        return ""
    names = sorted(p.name for p in assets.glob("index-*.js"))
    return ",".join(names)


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
        "node_count": get_node_count(),
        "schema_status": get_schema_status(),
        "dashboard_build": get_dashboard_build(),
    }
