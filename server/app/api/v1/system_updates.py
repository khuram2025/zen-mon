"""System update status and control endpoints for the dashboard."""

import configparser
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/system", tags=["System Updates"])

UPDATER_DIR = Path("/opt/zenplus/updater")
CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"
VERSION_FILE = Path("/opt/zenplus/.version")
LOG_FILE = UPDATER_DIR / "logs" / "update.log"


class UpdateConfig(BaseModel):
    auto_update: bool = True
    check_interval_hours: int = 4
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""


class UpdateHistoryRecord(BaseModel):
    version: str
    from_version: str
    status: str
    changelog: str = ""
    severity: str = "normal"
    error: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class UpdateStatus(BaseModel):
    current_version: str
    installed_at: str = ""
    appliance_id: str
    server_url: str
    auto_update: bool
    check_interval_hours: int
    maintenance_window_start: str
    maintenance_window_end: str
    last_check: str
    next_check: str
    timer_active: bool
    updater_running: bool = False
    last_update: Optional[UpdateHistoryRecord] = None
    active_update: Optional[UpdateHistoryRecord] = None
    history: list[UpdateHistoryRecord] = []
    recent_log: list[str] = []


def _get_version_info() -> tuple[str, str]:
    """Return (version, installed_at) from .version file."""
    version = "unknown"
    installed_at = ""
    if VERSION_FILE.exists():
        lines = VERSION_FILE.read_text().strip().splitlines()
        if lines:
            version = lines[0].strip()
        if len(lines) > 1:
            installed_at = lines[1].strip()
    return version, installed_at


def _read_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH)
    return parser


def _write_config(parser: configparser.ConfigParser) -> None:
    with open(CONFIG_PATH, "w") as f:
        parser.write(f)


def _get_timer_info() -> dict:
    """Get systemd timer status."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "zenplus-updater.timer",
             "--property=ActiveState,NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, text=True, timeout=5,
        )
        info = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        return {
            "active": info.get("ActiveState") == "active",
            "next": info.get("NextElapseUSecRealtime", ""),
            "last": info.get("LastTriggerUSec", ""),
        }
    except Exception:
        return {"active": False, "next": "", "last": ""}


@router.get("/update-status")
async def get_update_status(user: User = Depends(get_current_user)):
    """Get full update agent status including history and errors."""
    config = _read_config()
    timer = _get_timer_info()
    version, installed_at = _get_version_info()

    # Recent log lines
    recent_log = []
    if LOG_FILE.exists():
        try:
            all_lines = LOG_FILE.read_text().strip().splitlines()
            recent_log = all_lines[-30:]
        except Exception:
            pass

    # Load update history
    history_file = UPDATER_DIR / "logs" / "update-history.json"
    history_raw = []
    try:
        if history_file.exists():
            history_raw = json.loads(history_file.read_text())
            history_raw = list(reversed(history_raw))
    except (json.JSONDecodeError, OSError):
        pass

    # Find last completed + active update
    last_update = None
    active_update = None
    for r in history_raw:
        if r.get("status") in ("downloading", "applying") and not active_update:
            active_update = UpdateHistoryRecord(**r)
        if r.get("status") in ("success", "failed", "rolled_back") and not last_update:
            last_update = UpdateHistoryRecord(**r)

    # Check if updater is currently running
    updater_running = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "zenplus-updater.service"],
            capture_output=True, text=True, timeout=5,
        )
        updater_running = result.stdout.strip() == "activating"
    except Exception:
        pass

    interval_sec = config.getint("server", "check_interval_seconds", fallback=14400)

    return UpdateStatus(
        current_version=version,
        installed_at=installed_at,
        appliance_id=config.get("appliance", "id", fallback=""),
        server_url=config.get("server", "url", fallback="https://zentryc.com"),
        auto_update=config.getboolean("update", "auto_update", fallback=True),
        check_interval_hours=max(1, interval_sec // 3600),
        maintenance_window_start=config.get("update", "maintenance_window_start", fallback=""),
        maintenance_window_end=config.get("update", "maintenance_window_end", fallback=""),
        last_check=timer.get("last", ""),
        next_check=timer.get("next", ""),
        timer_active=timer.get("active", False),
        updater_running=updater_running,
        last_update=last_update,
        active_update=active_update,
        history=[UpdateHistoryRecord(**r) for r in history_raw[:10]],
        recent_log=recent_log,
    )


@router.put("/update-config")
async def update_config(body: UpdateConfig, user: User = Depends(get_current_user)):
    """Update the update agent configuration."""
    config = _read_config()

    if not config.has_section("update"):
        config.add_section("update")
    if not config.has_section("server"):
        config.add_section("server")

    config.set("update", "auto_update", str(body.auto_update))
    config.set("server", "check_interval_seconds", str(body.check_interval_hours * 3600))
    config.set("update", "maintenance_window_start", body.maintenance_window_start)
    config.set("update", "maintenance_window_end", body.maintenance_window_end)

    _write_config(config)

    # Update systemd timer interval
    timer_path = Path("/etc/systemd/system/zenplus-updater.timer")
    if timer_path.exists():
        try:
            content = timer_path.read_text()
            # Replace OnUnitActiveSec line
            import re
            content = re.sub(
                r"OnUnitActiveSec=.*",
                f"OnUnitActiveSec={body.check_interval_hours}h",
                content,
            )
            timer_path.write_text(content)
            subprocess.run(["systemctl", "daemon-reload"], timeout=10)
        except Exception:
            pass

    return {"status": "ok", "message": "Configuration updated"}


@router.post("/check-update")
async def trigger_check(user: User = Depends(get_current_user)):
    """Trigger an on-demand update check."""
    try:
        result = subprocess.run(
            ["systemctl", "start", "zenplus-updater.service"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to trigger: {result.stderr}")
        return {"status": "ok", "message": "Update check triggered"}
    except subprocess.TimeoutExpired:
        return {"status": "ok", "message": "Update check triggered (running)"}


# ─── Registration ─────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    license_key: str


class RegistrationStatus(BaseModel):
    registered: bool
    appliance_id: str
    server_url: str


@router.get("/registration")
async def get_registration(user: User = Depends(get_current_user)):
    """Get current registration status."""
    config = _read_config()
    appliance_id = config.get("appliance", "id", fallback="")
    return RegistrationStatus(
        registered=bool(appliance_id),
        appliance_id=appliance_id,
        server_url=config.get("server", "url", fallback="https://zentryc.com"),
    )


@router.post("/register")
async def register_appliance(body: RegisterRequest, user: User = Depends(get_current_user)):
    """Register this appliance with zentryc.com using a license key."""
    import httpx
    import platform
    from pathlib import Path

    config = _read_config()
    server_url = config.get("server", "url", fallback="https://zentryc.com")

    # Collect system info
    version, _ = _get_version_info()
    hostname = platform.node()

    # Get arch
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("x86_64", "amd64") else "arm64" if machine in ("aarch64", "arm64") else machine

    # Get OS version
    os_version = "linux"
    try:
        os_release = Path("/etc/os-release").read_text()
        info = {}
        for line in os_release.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip('"')
        os_version = f"{info.get('ID', 'linux')}-{info.get('VERSION_ID', '')}"
    except OSError:
        pass

    # Call zentryc.com registration API
    try:
        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            resp = await client.post(
                f"{server_url}/api/v1/appliances/register",
                json={
                    "hostname": hostname,
                    "arch": arch,
                    "os_version": os_version,
                    "current_version": version,
                    "registration_token": body.license_key,
                },
            )

        if resp.status_code != 200:
            error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_data.get("error") or error_data.get("detail") or resp.text
            raise HTTPException(status_code=resp.status_code, detail=str(error_msg))

        data = resp.json()
        appliance_id = data.get("appliance_id", "")
        api_key = data.get("api_key", "")

        if not appliance_id or not api_key:
            raise HTTPException(status_code=500, detail="Server returned incomplete registration data")

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach update server: {e}")

    # Save credentials to agent.conf
    if not config.has_section("appliance"):
        config.add_section("appliance")
    config.set("appliance", "id", appliance_id)
    config.set("appliance", "api_key", api_key)
    _write_config(config)

    # Secure the config file
    import os
    os.chmod(str(CONFIG_PATH), 0o600)

    return {
        "status": "ok",
        "appliance_id": appliance_id,
        "message": "Appliance registered successfully",
    }


@router.get("/health")
async def system_health():
    """Comprehensive health check."""
    checks = {}
    checks["api"] = "ok"

    # PostgreSQL
    try:
        result = subprocess.run(
            ["pg_isready", "-h", "127.0.0.1", "-p", "5432"],
            capture_output=True, text=True, timeout=5,
        )
        checks["postgresql"] = "ok" if result.returncode == 0 else "error"
    except Exception:
        checks["postgresql"] = "error"

    # ClickHouse
    try:
        import httpx as _httpx
        r = _httpx.get("http://127.0.0.1:8123/ping", timeout=3)
        checks["clickhouse"] = "ok" if r.status_code == 200 else "error"
    except Exception:
        checks["clickhouse"] = "error"

    # Redis
    try:
        result = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "ping"],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.strip()
        checks["redis"] = "ok" if out == "PONG" or "NOAUTH" in out else "error"
    except Exception:
        checks["redis"] = "error"

    # Services
    for svc in ["zenplus-api", "zenplus-poller", "netmon-gunicorn", "nginx"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            checks[svc] = result.stdout.strip()
        except Exception:
            checks[svc] = "unknown"

    overall = "ok" if all(v in ("ok", "active") for v in checks.values()) else "degraded"
    version, _ = _get_version_info()

    return {"status": overall, "version": version, "checks": checks}
